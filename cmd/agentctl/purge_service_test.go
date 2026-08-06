package main

// The CLI layer of `agentctl purge-service` (HEALTH-005 PR-B), covered separately from the rewrite it
// calls.
//
// Written for the same reason purge_cli_test.go was: internal/svclog/purge_test.go proves the REWRITE
// keeps and removes the right records, and nothing there knows what a confirmation flag is. The
// `--yes` guard and the self-recording have no counterpart below this layer, so if they are not
// tested here they are not tested anywhere — and the surface gate only greps the source for them,
// which is a check on a spelling rather than on a behaviour.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/AlexGromer/sentinel/internal/svclog"
)

// seedJournal writes one record dated `age` ago and returns the repo it lives under.
func seedJournal(t *testing.T, age time.Duration, msg string) string {
	t.Helper()
	repo := t.TempDir()
	w := svclog.Open(filepath.Join(repo, "state"), "control-api")
	if w == nil {
		t.Fatal("could not open a journal to seed")
	}
	w.Log(svclog.Record{
		TS:  time.Now().Add(-age).UTC().Format(time.RFC3339Nano),
		Lvl: "info", Cat: "service", Code: "service.login_ok", Msg: msg,
	})
	w.Close()
	return repo
}

func journalRecords(t *testing.T, repo string) []svclog.Record {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(repo, "state", "logs", svclog.FileName))
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		t.Fatal(err)
	}
	var out []svclog.Record
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if line == "" {
			continue
		}
		var r svclog.Record
		if err := json.Unmarshal([]byte(line), &r); err != nil {
			t.Fatalf("journal line is not JSON: %s", line)
		}
		out = append(out, r)
	}
	return out
}

// TestPurgeServiceRefusesWithoutConfirmation.
// Kills: dropping the --yes guard, which nothing below this layer knows about.
func TestPurgeServiceRefusesWithoutConfirmation(t *testing.T) {
	repo := seedJournal(t, time.Hour, "must survive a refusal")

	if code := cmdPurgeService(repo, nil); code != 2 {
		t.Errorf("purge-service with no --yes exited %d, want the usage code 2", code)
	}
	recs := journalRecords(t, repo)
	if len(recs) != 1 || recs[0].Msg != "must survive a refusal" {
		t.Errorf("a refused purge destroyed records anyway: %+v", recs)
	}
}

// TestPurgeServiceWritesDownThatItHappened.
// Kills: removing the service.log_purged record. A journal whose deletion leaves no trace answers
// "was anything removed?" with silence, and silence is what somebody destroying evidence wants.
func TestPurgeServiceWritesDownThatItHappened(t *testing.T) {
	repo := seedJournal(t, 100*time.Hour, "old enough to go")

	if code := cmdPurgeService(repo, []string{"--yes"}); code != 0 {
		t.Fatalf("purge-service exited %d", code)
	}
	recs := journalRecords(t, repo)
	if len(recs) != 1 {
		t.Fatalf("want exactly the purge's own record left, got %d: %+v", len(recs), recs)
	}
	got := recs[0]
	if got.Code != "service.log_purged" {
		t.Errorf("the purge left %q rather than a record of itself", got.Code)
	}
	if got.Lvl != "warn" {
		t.Errorf("service.log_purged written at %q — destroying audit records is the line somebody "+
			"comes looking for, which is what warn means in this stream", got.Lvl)
	}
	if got.Svc != "agentctl" {
		t.Errorf("the record does not name WHO purged: %+v", got)
	}
	// The counts have to be IN it. "The journal was purged" without a number is a sentence an
	// operator cannot check against anything.
	if !strings.Contains(got.Msg, "1") {
		t.Errorf("the record does not say how many records were removed: %q", got.Msg)
	}
}

// TestPurgeServiceHonoursTheCutoff.
// Kills: ignoring --older-than, which would turn "clean up last month" into "destroy everything" —
// the failure whose report looks identical to a correct run.
func TestPurgeServiceHonoursTheCutoff(t *testing.T) {
	repo := seedJournal(t, time.Hour, "recent, must survive")

	if code := cmdPurgeService(repo, []string{"--yes", "--older-than", "24h"}); code != 0 {
		t.Fatalf("purge-service exited %d", code)
	}
	var kept, purgeRecord int
	for _, r := range journalRecords(t, repo) {
		if r.Msg == "recent, must survive" {
			kept++
		}
		if r.Code == "service.log_purged" {
			purgeRecord++
		}
	}
	if kept != 1 {
		t.Error("a record newer than --older-than was destroyed")
	}
	if purgeRecord != 1 {
		t.Error("the purge did not record itself when it removed nothing — an operator cannot tell " +
			"'nothing was old enough' from 'the command never ran'")
	}
}

// TestPurgeServiceWithNoJournalSaysSoAndSucceeds.
// Kills: reporting an error for a deployment that has simply never written a journal, which would
// make the command unusable in exactly the fresh install where an operator first tries it.
func TestPurgeServiceWithNoJournalSaysSoAndSucceeds(t *testing.T) {
	if code := cmdPurgeService(t.TempDir(), []string{"--yes"}); code != 0 {
		t.Errorf("purging an absent journal exited %d, want 0", code)
	}
}
