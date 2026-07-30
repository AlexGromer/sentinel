package main

// ADR-109 second half — the chats projection wrote into a database nobody read.
//
// `agentctl` always started its OWN store-gateway over repo/state/locators.db and injected that
// address as a run-var, which is appended AFTER filteredEnv and therefore overrode any inherited
// STORE_ADDR. control-api runs its own gateway on CONTROL_API_STORE_ADDR. Two databases, one
// projection: the brain wrote the `chats` row into locators.db while the API read its own store, so
// `GET /v1/chats` answered 0 — truthfully, about a table nobody had ever written to.
//
// The check is BEHAVIOURAL, not a pinned predicate. A pure "which gateway should we use?" function
// would keep returning the right answer while runWithStore ignored it, and that is precisely the
// shape of the bug being closed: the decision existed, the caller overrode it. So this runs
// runWithStore for real, with a stand-in for the brain that records the environment it was handed.

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// brainStub writes its environment into <repo>/env.txt (cmd.Dir is the repo, so $PWD is it) and
// exits 0. spawnBrain honours BRAIN_PYTHON, which survives the env allowlist.
func brainStub(t *testing.T, dir string) string {
	t.Helper()
	path := filepath.Join(dir, "fake-brain.sh")
	script := "#!/bin/sh\nenv > \"$PWD/env.txt\"\nexit 0\n"
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestRunWithStoreHonoursAnInheritedGateway(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the brain stub is a /bin/sh script")
	}
	repo := t.TempDir()
	const addr = "unix:/tmp/sentinel-test-inherited.sock"
	const tok = "inherited-tok"
	t.Setenv("STORE_ADDR", addr)
	t.Setenv("STORE_TOKEN", tok)
	t.Setenv("BRAIN_PYTHON", brainStub(t, repo))

	if rc := runWithStore(repo, "r1", []string{"RUN_MODE=chat"}); rc != 0 {
		t.Fatalf("runWithStore = %d, want 0", rc)
	}

	env, err := os.ReadFile(filepath.Join(repo, "env.txt"))
	if err != nil {
		t.Fatalf("the brain stub did not run: %v", err)
	}
	got := string(env)
	if !strings.Contains(got, "STORE_ADDR="+addr) {
		t.Errorf("the brain was not given the inherited STORE_ADDR (%s).\nThis is the defect: agentctl "+
			"started its own gateway and the projection landed in a second database.\nenv:\n%s", addr, got)
	}
	// STORE_TOKEN is deliberately NOT in filteredEnv's allowlist — it is normally a per-run secret minted
	// here — so inheriting the ADDRESS without forwarding the token would hand the brain a gateway it
	// cannot authenticate against. The chats projection is best-effort and would have swallowed that
	// error exactly as it swallowed the last one.
	if !strings.Contains(got, "STORE_TOKEN="+tok) {
		t.Errorf("the inherited STORE_TOKEN did not reach the brain; the gateway would refuse every call "+
			"and the best-effort projection would report nothing.\nenv:\n%s", got)
	}
	// No second gateway: startGateway creates its socket under <repo>/state, so the absence of that
	// directory's sockets is the observable fact that none was started.
	entries, _ := os.ReadDir(filepath.Join(repo, "state"))
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "sentinel-store-") {
			t.Errorf("a second gateway was started (%s) despite an inherited address", e.Name())
		}
	}
}

// TestRunWithStoreStillStartsItsOwnGatewayWhenAlone is the other half. Without it, "honour the
// inherited address" could be satisfied by never starting a gateway at all — which would silently
// drop every standalone `agentctl run --mode chat` back to the storeless path this milestone's
// predecessor (SEC-CHATS-WIRING-GAP) fixed.
func TestRunWithStoreStillStartsItsOwnGatewayWhenAlone(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the brain stub is a /bin/sh script")
	}
	repo := t.TempDir()
	os.Unsetenv("STORE_ADDR")
	os.Unsetenv("STORE_TOKEN")
	t.Setenv("BRAIN_PYTHON", brainStub(t, repo))

	// A gateway binary that reports a socket the way the real one does, so startGateway's wait
	// succeeds without this test depending on a built store-gateway.
	gw := filepath.Join(repo, "bin", "store-gateway")
	if err := os.MkdirAll(filepath.Dir(gw), 0o755); err != nil {
		t.Fatal(err)
	}
	// $2 is the --addr value: create the socket path as a plain file, then idle until killed.
	stub := "#!/bin/sh\ntouch \"$2\"\nwhile true; do sleep 1; done\n"
	if err := os.WriteFile(gw, []byte(stub), 0o755); err != nil {
		t.Fatal(err)
	}

	if rc := runWithStore(repo, "r2", []string{"RUN_MODE=chat"}); rc != 0 {
		t.Fatalf("runWithStore = %d, want 0", rc)
	}
	env, err := os.ReadFile(filepath.Join(repo, "env.txt"))
	if err != nil {
		t.Fatalf("the brain stub did not run: %v", err)
	}
	got := string(env)
	if !strings.Contains(got, "STORE_ADDR="+filepath.Join(repo, "state")) {
		t.Errorf("with no inherited address the run must get its OWN gateway under <repo>/state.\nenv:\n%s", got)
	}
	if !strings.Contains(got, "STORE_TOKEN=") {
		t.Errorf("a self-started gateway mints a per-run token and hands it to the brain (#23).\nenv:\n%s", got)
	}
}
