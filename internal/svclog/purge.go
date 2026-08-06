package svclog

// HEALTH-005 PR-B — destroying journal records, as an operator command that says what it did.
//
// The retention posture chosen in PR-A and unchanged here: NOTHING is deleted automatically. Rotation
// bounds the disk; removal is something a person asks for by name (`agentctl purge-service`), the
// same posture as ADR-098's `redact-trace` and ADR-100's `purge-store`, and for a sharper reason —
// an audit record that removes itself is not evidence.
//
// The logic lives HERE rather than in the CLI because this package owns the file: its format, its two
// generations, its permissions and its lock. A purge written in cmd/agentctl would be a second piece
// of code that believes it knows the journal's layout, and the first divergence would be silent.
//
// TWO RULES, both of which cost lines and both of which are the point:
//
//   - A record whose timestamp cannot be read is KEPT and counted separately. "Older than" is a
//     question about a record we could not place in time, so the honest answer is to leave it and say
//     how many there were. Destroying the unclassifiable is the one thing an audit purge must not do
//     quietly.
//   - Whatever was appended WHILE the purge was reading is carried over unfiltered. It is newer than
//     any cutoff by construction, and this is what protects a writer that does not take the lock —
//     the browser service writes from Node, and Node has no flock without a native module this
//     project does not take.

import (
	"bytes"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"time"
)

// PurgeReport is what the operator is told: counts, never content. A tool that printed what it
// deleted would be a second copy of whatever leaked.
type PurgeReport struct {
	Removed int // records destroyed
	Kept    int // records still in the file afterwards
	Undated int // kept BECAUSE their timestamp could not be read (subset of Kept)
	Files   []string
}

// Purge removes every record older than `cutoff` from both generations of the journal under
// <stateDir>/logs. A generation that does not exist is skipped, not an error: the rotated half
// usually does not exist, and a deployment that has never rotated must not see a failure for it.
func Purge(stateDir string, cutoff time.Time) (PurgeReport, error) {
	dir := filepath.Join(stateDir, "logs")
	var total PurgeReport
	// Oldest generation first, so a failure part-way leaves the RECENT half intact — the half an
	// operator is more likely to still need.
	for _, name := range []string{Rotated, FileName} {
		path := filepath.Join(dir, name)
		rep, err := purgeOne(path, cutoff)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return total, err
		}
		total.Removed += rep.Removed
		total.Kept += rep.Kept
		total.Undated += rep.Undated
		total.Files = append(total.Files, path)
	}
	return total, nil
}

// purgeAfterSnapshot is a test seam at the ONE point where a writer that does NOT take the lock can
// slip a record in: after the snapshot has been read and filtered, before the file is truncated.
//
// It exists because that window is real and is about to be occupied. The browser service writes this
// journal from Node (PR-C), and Node has no flock without a native module this project will not take,
// so the carry-over below is the only thing protecting its records. Measured with a lock-taking
// writer, the carry-over is dead code — the writer simply blocks — and a timing-based test of it
// passes with the carry-over deleted. This makes the window deterministic instead.
var purgeAfterSnapshot func()

func purgeOne(path string, cutoff time.Time) (PurgeReport, error) {
	f, err := os.OpenFile(path, os.O_RDWR, 0o640)
	if err != nil {
		return PurgeReport{}, err
	}
	defer f.Close()
	unlock := lockFile(f)
	defer unlock()

	orig, err := io.ReadAll(f)
	if err != nil {
		return PurgeReport{}, err
	}

	var rep PurgeReport
	var kept bytes.Buffer
	keep := func(line []byte) {
		rep.Kept++
		kept.Write(line)
		kept.WriteByte('\n')
	}
	for _, line := range bytes.Split(orig, []byte("\n")) {
		if len(bytes.TrimSpace(line)) == 0 {
			continue
		}
		var r Record
		if json.Unmarshal(line, &r) != nil {
			rep.Undated++
			keep(line)
			continue
		}
		ts, terr := time.Parse(time.RFC3339Nano, r.TS)
		if terr != nil {
			rep.Undated++
			keep(line)
			continue
		}
		if ts.Before(cutoff) {
			rep.Removed++
			continue
		}
		keep(line)
	}

	if purgeAfterSnapshot != nil {
		purgeAfterSnapshot()
	}

	// Anything appended since the read above. The descriptor's offset is already at the end of what we
	// read, so a second ReadAll returns exactly the new bytes and nothing else.
	if tail, terr := io.ReadAll(f); terr == nil && len(bytes.TrimSpace(tail)) > 0 {
		kept.Write(tail)
		if !bytes.HasSuffix(tail, []byte("\n")) {
			kept.WriteByte('\n')
		}
	}

	if err := f.Truncate(0); err != nil {
		return rep, err
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return rep, err
	}
	if _, err := f.Write(kept.Bytes()); err != nil {
		return rep, err
	}
	return rep, nil
}
