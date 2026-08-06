package svclog

// HEALTH-005 PR-B — the gate on destroying journal records.
//
// The concurrency test is the one that earns its place. Every other property here would also hold for
// a naive read-filter-rewrite, and that version loses records written during the rewrite — silently,
// in the one file whose whole purpose is that nothing about it is silent.

import (
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// journalLines returns the raw lines of a generation.
func journalLines(t *testing.T, stateDir, name string) []string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(stateDir, "logs", name))
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		t.Fatal(err)
	}
	s := strings.TrimSpace(string(b))
	if s == "" {
		return nil
	}
	return strings.Split(s, "\n")
}

// seed writes records with explicit timestamps straight into a generation.
func seed(t *testing.T, stateDir, name string, recs []Record) {
	t.Helper()
	dir := filepath.Join(stateDir, "logs")
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	w := Open(stateDir, "seed")
	if w == nil {
		t.Fatal("could not open the journal to seed it")
	}
	defer w.Close()
	for _, r := range recs {
		w.Log(r)
	}
	if name == Rotated {
		if err := os.Rename(filepath.Join(dir, FileName), filepath.Join(dir, Rotated)); err != nil {
			t.Fatal(err)
		}
	}
}

func ts(d time.Duration) string { return time.Now().Add(d).UTC().Format(time.RFC3339Nano) }

func TestPurgeRemovesTheOldAndKeepsTheRecent(t *testing.T) {
	dir := t.TempDir()
	seed(t, dir, FileName, []Record{
		{TS: ts(-100 * time.Hour), Lvl: "info", Code: "service.started", Msg: "ancient"},
		{TS: ts(-1 * time.Hour), Lvl: "info", Code: "service.login_ok", Msg: "recent"},
	})

	rep, err := Purge(dir, time.Now().Add(-24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if rep.Removed != 1 || rep.Kept != 1 {
		t.Fatalf("removed=%d kept=%d, want 1/1", rep.Removed, rep.Kept)
	}
	lines := journalLines(t, dir, FileName)
	if len(lines) != 1 || !strings.Contains(lines[0], "recent") {
		t.Errorf("the wrong record survived: %v", lines)
	}
}

func TestPurgeReachesTheRotatedGenerationToo(t *testing.T) {
	// A purge that only cleaned the current file would leave the older half on disk while reporting
	// success — the operator's belief and the disk would disagree, which is worse than not purging.
	dir := t.TempDir()
	seed(t, dir, Rotated, []Record{{TS: ts(-100 * time.Hour), Lvl: "info", Code: "service.started", Msg: "old gen"}})
	seed(t, dir, FileName, []Record{{TS: ts(-100 * time.Hour), Lvl: "info", Code: "service.started", Msg: "current gen"}})

	rep, err := Purge(dir, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Removed != 2 {
		t.Errorf("removed=%d, want both generations (2)", rep.Removed)
	}
	if l := journalLines(t, dir, Rotated); len(l) != 0 {
		t.Errorf("the rotated generation survived the purge: %v", l)
	}
}

func TestARecordThatCannotBePlacedInTimeIsKeptAndCounted(t *testing.T) {
	// "Older than" is a question an undated record cannot be asked, so the answer is not "yes".
	dir := t.TempDir()
	logs := filepath.Join(dir, "logs")
	if err := os.MkdirAll(logs, 0o750); err != nil {
		t.Fatal(err)
	}
	body := `{"seq":1,"ts":"not-a-timestamp","lvl":"info","cat":"service","code":"service.started","msg":"undated"}` + "\n" +
		`this line is not JSON at all` + "\n" +
		`{"seq":2,"ts":"` + ts(-100*time.Hour) + `","lvl":"info","cat":"service","code":"service.started","msg":"old"}` + "\n"
	if err := os.WriteFile(filepath.Join(logs, FileName), []byte(body), 0o640); err != nil {
		t.Fatal(err)
	}

	rep, err := Purge(dir, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Removed != 1 {
		t.Errorf("removed=%d, want only the one dated record", rep.Removed)
	}
	if rep.Undated != 2 || rep.Kept != 2 {
		t.Errorf("undated=%d kept=%d — both the unparseable line and the bad timestamp must be kept AND "+
			"counted, or the report is a claim the operator cannot check", rep.Undated, rep.Kept)
	}
	if len(journalLines(t, dir, FileName)) != 2 {
		t.Error("the kept records are not on disk")
	}
}

func TestPurgeOfAnAbsentJournalIsNotAnError(t *testing.T) {
	rep, err := Purge(t.TempDir(), time.Now())
	if err != nil {
		t.Fatalf("purging a journal that was never written failed: %v", err)
	}
	if len(rep.Files) != 0 || rep.Removed != 0 {
		t.Errorf("something was reported for an absent journal: %+v", rep)
	}
}

func TestARecordAppendedDuringThePurgeIsNotLost(t *testing.T) {
	// The property the lock and the carry-over exist for, measured rather than asserted about the code.
	// A naive read-filter-rewrite drops whatever lands between the read and the write, and nothing about
	// the report would say so: the counts would be perfectly consistent with themselves.
	//
	// Every appended record is NEW (its timestamp is now), so the cutoff below would keep all of them
	// anyway — which is what makes a missing one unambiguous evidence of the race rather than of the
	// filter.
	dir := t.TempDir()
	var old []Record
	for i := 0; i < 500; i++ {
		old = append(old, Record{TS: ts(-100 * time.Hour), Lvl: "info", Code: "service.api_call", Msg: "old record"})
	}
	seed(t, dir, FileName, old)

	w := Open(dir, "concurrent-writer")
	if w == nil {
		t.Fatal("could not open a second writer")
	}
	defer w.Close()

	const appended = 200
	var wg sync.WaitGroup
	wg.Add(1)
	start := make(chan struct{})
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < appended; i++ {
			w.Log(Record{Lvl: "info", Cat: "service", Code: "service.login_ok", Msg: "written during the purge"})
		}
	}()

	close(start)
	rep, err := Purge(dir, time.Now().Add(-24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	wg.Wait()

	var survived int
	for _, l := range journalLines(t, dir, FileName) {
		if strings.Contains(l, "written during the purge") {
			survived++
		}
	}
	if survived != appended {
		t.Errorf("%d of %d records appended during the purge survived — the rewrite ate audit records, "+
			"and the report (%+v) says nothing about it", survived, appended, rep)
	}
}

func TestARecordFromAWriterThatTakesNoLockIsCarriedOver(t *testing.T) {
	// The case the carry-over exists for, made deterministic. The test above cannot reach it: a Go
	// writer takes the lock and simply blocks, so with a lock in place the carry-over is never
	// exercised — measured, by deleting the carry-over and watching that test stay green.
	//
	// The browser service writes this journal from Node, which takes no lock. That appender is
	// simulated here with a raw O_APPEND write at exactly the moment one could arrive.
	dir := t.TempDir()
	seed(t, dir, FileName, []Record{
		{TS: ts(-100 * time.Hour), Lvl: "info", Cat: "service", Code: "service.api_call", Msg: "old record"},
	})
	path := filepath.Join(dir, "logs", FileName)

	const line = `{"seq":0,"ts":"","lvl":"info","cat":"service","code":"service.started","msg":"from a writer that takes no lock"}`
	purgeAfterSnapshot = func() {
		f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0o640)
		if err != nil {
			t.Fatal(err)
		}
		defer f.Close()
		if _, err := f.WriteString(line + "\n"); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { purgeAfterSnapshot = nil })

	if _, err := Purge(dir, time.Now()); err != nil {
		t.Fatal(err)
	}
	var found bool
	for _, l := range journalLines(t, dir, FileName) {
		if strings.Contains(l, "from a writer that takes no lock") {
			found = true
		}
	}
	if !found {
		t.Error("a record appended during the rewrite by a writer that takes no lock was destroyed — " +
			"which is every record the browser service writes while a purge is running")
	}
}

func TestTheWriterDoesNotRotateEarlyAfterAPurgeShrankTheFile(t *testing.T) {
	// A size carried in a counter is wrong the moment anything else changes the file. The consequence is
	// invisible and in the worst direction: the journal rotates on its next write and throws away the
	// generation somebody was about to read.
	dir := t.TempDir()
	t.Setenv("SENTINEL_SERVICE_LOG_MAX_MB", "1")
	w := Open(dir, "control-api")
	if w == nil {
		t.Fatal("could not open the journal")
	}
	defer w.Close()
	// Fill most of the 1 MiB cap with removable records.
	filler := strings.Repeat("x", 1000)
	for i := 0; i < 900; i++ {
		w.Log(Record{TS: ts(-100 * time.Hour), Lvl: "info", Cat: "service", Code: "service.api_call", Msg: filler})
	}
	if _, err := Purge(dir, time.Now().Add(-24*time.Hour)); err != nil {
		t.Fatal(err)
	}
	// The file is now nearly empty. One more write must NOT rotate.
	w.Log(Record{Lvl: "info", Cat: "service", Code: "service.login_ok", Msg: "after the purge"})
	if _, err := os.Stat(filepath.Join(dir, "logs", Rotated)); err == nil {
		t.Error("the writer rotated after a purge shrank the file — the recent generation was discarded " +
			"because a counter, not the file, was believed about its size")
	}
	lines := journalLines(t, dir, FileName)
	if len(lines) != 1 || !strings.Contains(lines[0], "after the purge") {
		t.Errorf("want exactly the one post-purge record, got %d line(s)", len(lines))
	}
}
