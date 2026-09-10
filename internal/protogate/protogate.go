// Package protogate decides whether an absent protoc toolchain is a skip or a failure.
//
// WHY IT EXISTS. Both stub-reproducibility gates — internal/store/pb/regen_test.go and
// internal/orchestrator/pb/regen_test.go — opened with two environment probes (a project venv, then
// protoc-gen-go and protoc-gen-go-grpc on PATH) and called t.Skipf when either missed. In CI neither
// was ever satisfied: no workflow step installs the Go plugins. A skipped Go test is a GREEN test —
// `go test ./...` prints ok and the step passes — so ADR-109's property ("the committed stubs are
// reproducible from proto/*.proto") has never once been compared in CI, while the gate that claims it
// reported success on every run since M2b.
//
// THE RULE. A skip may be a developer convenience; it may never be CI's answer. Where the toolchain
// is DECLARED present, its absence is a failure, because there the skip is not "we cannot check this
// here" but "we did not check the thing this file exists to check".
//
// The declaration is an explicit signal (SENTINEL_REQUIRE_PROTO_GATE=1), not a sniff at CI=true:
// the environment that promises the toolchain is the one that installs it, and those two facts
// belong in the same place — the workflow step. A `CI` sniff would also turn every fork's and every
// unrelated runner's build red for a toolchain nobody there promised.
package protogate

import (
	"os"
	"testing"
)

// EnvRequire names the signal that turns a missing prerequisite into a failure. The workflow step
// that installs the plugins sets it; nothing else should.
const EnvRequire = "SENTINEL_REQUIRE_PROTO_GATE"

// Required reports whether this environment has declared that the protoc toolchain is present.
func Required() bool { return os.Getenv(EnvRequire) == "1" }

// Missing records an absent prerequisite: a failure where the toolchain was promised, a skip where it
// was not. Callers pass what is missing and how to get it.
func Missing(t *testing.T, what, remedy string) {
	t.Helper()
	if Required() {
		t.Fatalf("%s — but %s=1 declares this environment has the protoc toolchain, so skipping here "+
			"would report success over a contract nothing compared. Either install it (%s) or stop "+
			"setting %s.", what, EnvRequire, remedy, EnvRequire)
	}
	t.Skipf("%s — %s. (Set %s=1 to make this a failure instead; CI does.)", what, remedy, EnvRequire)
}
