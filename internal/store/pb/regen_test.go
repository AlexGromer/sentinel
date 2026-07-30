package pb_test

// ADR-109 / GAP-RISK-008: the generated stubs are REPRODUCIBLE from proto/*.proto, and this proves it
// by regenerating them into a temp dir and diffing byte-for-byte against what is committed.
//
// The gap this closes has been open since M2b: the stubs say "DO NOT EDIT", nothing checked that they
// had not been, and nothing checked they still matched the .proto. Either failure is silent — a stub
// edited by hand compiles, and a .proto changed without regeneration compiles too, right up until a
// field is read that one side has and the other does not.
//
// protoc comes from grpc_tools inside the project venv, NOT from PATH. That is deliberate: it bundles
// libprotoc 33.5, which is the exact version the committed headers name, so this gate compares like
// with like instead of reporting a version banner as drift. It also means the check needs no system
// package and works in the air-gapped build.

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

func TestGeneratedStubsAreReproducible(t *testing.T) {
	root := repoRoot(t)
	py := filepath.Join(root, ".venv", "bin", "python")
	if _, err := os.Stat(py); err != nil {
		t.Skipf("no project venv at %s — the brain env provides protoc, so there is nothing to compare against", py)
	}
	// The Go plugins are separate binaries protoc execs by name. Without them protoc reports a
	// confusing "plugin not found", so say plainly what is missing instead.
	for _, plugin := range []string{"protoc-gen-go", "protoc-gen-go-grpc"} {
		if _, err := exec.LookPath(plugin); err != nil {
			t.Skipf("%s not on PATH — regeneration needs it (go install google.golang.org/protobuf/cmd/protoc-gen-go@latest and .../grpc/cmd/protoc-gen-go-grpc@latest)", plugin)
		}
	}

	out := t.TempDir()
	cmd := exec.Command(py, "-m", "grpc_tools.protoc", "-Iproto",
		"--go_out="+out, "--go-grpc_out="+out, "proto/store.proto", "proto/persistence.proto")
	cmd.Dir = root
	if combined, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("regeneration failed: %v\n%s", err, combined)
	}

	genDir := filepath.Join(out, "github.com", "AlexGromer", "sentinel", "internal", "store", "pb")
	entries, err := os.ReadDir(genDir)
	if err != nil {
		t.Fatalf("protoc wrote nothing to %s: %v", genDir, err)
	}
	if len(entries) == 0 {
		t.Fatal("protoc produced no files — this gate would pass by comparing nothing")
	}
	committedDir := filepath.Join(root, "internal", "store", "pb")
	for _, e := range entries {
		fresh, err := os.ReadFile(filepath.Join(genDir, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		committed, err := os.ReadFile(filepath.Join(committedDir, e.Name()))
		if err != nil {
			t.Errorf("%s was generated but is not committed: %v", e.Name(), err)
			continue
		}
		if string(fresh) != string(committed) {
			t.Errorf("%s differs from a fresh generation — either the stub was hand-edited or proto/ "+
				"changed without regenerating. Fix by running, from the repo root:\n"+
				"  .venv/bin/python -m grpc_tools.protoc -Iproto --go_out=. --go-grpc_out=. proto/store.proto proto/persistence.proto\n"+
				"  (then move github.com/AlexGromer/sentinel/internal/store/pb/* into place)", e.Name())
		}
	}
}
