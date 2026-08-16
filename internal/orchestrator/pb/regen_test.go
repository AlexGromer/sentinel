package pb_test

// ADR-126: the RunControl stubs are REPRODUCIBLE from proto/runcontrol.proto, proven by regenerating
// them into a temp dir and diffing byte-for-byte against what is committed.
//
// ⚠ WHY THIS FILE APPEARS ONLY NOW. `internal/store/pb` has had exactly this gate since ADR-109
// (GAP-RISK-008) and `internal/orchestrator/pb` never did — found while wiring the orchestrator, not
// by a check. So for the whole life of the RunControl contract, its stubs said "DO NOT EDIT" with
// nothing enforcing it, and `proto/runcontrol.proto` could have changed without regeneration. Both
// failures are silent: a hand-edited stub compiles, and a stale one compiles too, right up to the
// first read of a field one side has and the other does not.
//
// protoc comes from grpc_tools inside the project venv, NOT from PATH — same reason as the store
// gate: it bundles the exact libprotoc the committed headers name, so this compares like with like
// instead of reporting a version banner as drift, and it needs no system package in the air-gapped
// build.

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// repoRoot walks up from this package to the directory holding go.mod.
func repoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 8; i++ {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		dir = filepath.Dir(dir)
	}
	t.Fatal("could not find the repo root (no go.mod above this package)")
	return ""
}

func TestRunControlStubsAreReproducible(t *testing.T) {
	root := repoRoot(t)
	py := filepath.Join(root, ".venv", "bin", "python")
	if _, err := os.Stat(py); err != nil {
		t.Skipf("no project venv at %s — the brain env provides protoc, so there is nothing to compare against", py)
	}
	for _, plugin := range []string{"protoc-gen-go", "protoc-gen-go-grpc"} {
		if _, err := exec.LookPath(plugin); err != nil {
			t.Skipf("%s not on PATH — regeneration needs it (go install google.golang.org/protobuf/cmd/protoc-gen-go@latest and .../grpc/cmd/protoc-gen-go-grpc@latest)", plugin)
		}
	}

	out := t.TempDir()
	cmd := exec.Command(py, "-m", "grpc_tools.protoc", "-Iproto",
		"--go_out="+out, "--go-grpc_out="+out, "proto/runcontrol.proto")
	cmd.Dir = root
	if combined, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("regeneration failed: %v\n%s", err, combined)
	}

	genDir := filepath.Join(out, "github.com", "AlexGromer", "sentinel", "internal", "orchestrator", "pb")
	entries, err := os.ReadDir(genDir)
	if err != nil {
		t.Fatalf("protoc wrote nothing to %s: %v", genDir, err)
	}
	if len(entries) == 0 {
		t.Fatal("protoc produced no files — this gate would pass by comparing nothing")
	}
	committedDir := filepath.Join(root, "internal", "orchestrator", "pb")
	fresh := map[string]bool{}
	for _, e := range entries {
		fresh[e.Name()] = true
		got, err := os.ReadFile(filepath.Join(genDir, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		committed, err := os.ReadFile(filepath.Join(committedDir, e.Name()))
		if err != nil {
			t.Errorf("%s was generated but is not committed: %v", e.Name(), err)
			continue
		}
		if string(got) != string(committed) {
			t.Errorf("%s differs from a fresh generation — either the stub was hand-edited or "+
				"proto/runcontrol.proto changed without regenerating. Fix by running, from the repo root:\n"+
				"  .venv/bin/python -m grpc_tools.protoc -Iproto --go_out=. --go-grpc_out=. proto/runcontrol.proto\n"+
				"  (then move github.com/AlexGromer/sentinel/internal/orchestrator/pb/* into place)", e.Name())
		}
	}

	// ⚠ THE OTHER DIRECTION, which the store gate does not check and which is why it is here. Walking
	// only the freshly generated files answers "is what protoc produces committed?" — it cannot answer
	// "is everything committed something protoc produces?". A `.pb.go` left behind after a message was
	// deleted from the proto keeps compiling and keeps being imported; it is a stub for a contract that
	// no longer exists, and nothing about it looks wrong.
	committedEntries, err := os.ReadDir(committedDir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range committedEntries {
		if e.IsDir() || filepath.Ext(e.Name()) != ".go" || len(e.Name()) > 8 && e.Name()[len(e.Name())-8:] == "_test.go" {
			continue
		}
		if !fresh[e.Name()] {
			t.Errorf("%s is committed but a fresh generation does not produce it — a stub whose source "+
				"was removed from proto/runcontrol.proto still compiles and still gets imported", e.Name())
		}
	}
}
