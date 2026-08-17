//go:build !windows

package main

// dieOfSignal — the second half of the shutdown obituary (HEALTH-005).
//
// The handler writes "service.stopped" and then has to actually END the process, and HOW it ends is
// not cosmetic: a supervisor reads the exit status to decide whether the stop was expected.
// Re-raising the same signal after `signal.Stop` restores the default disposition, so the process
// dies exactly as it would have without a handler — `systemctl stop` sees a clean SIGTERM death and
// not an exit code the unit would report as a failure.
//
// ⚠ SPLIT BY PLATFORM BECAUSE `syscall.Kill` DOES NOT EXIST ON WINDOWS, and the way that was
// discovered is the reason this file carries a comment at all: it broke the cross-build on
// 2026-08-06 and nobody saw it for eleven days. `ci.yml` compiles Go for the HOST only, the
// cross-build lives solely in `release.yml`, and that had not run since 2026-08-02. The
// `install-ps1-smoke` job does run on windows-latest — but it exercises `install.ps1` against a fake
// release and never invokes the Go toolchain. So the first thing to notice was a failed release.
// The gate added with this fix (`GOOS=windows go build ./...` in ci.yml) is the actual repair; this
// split is only what makes the code compile.
//
// The shape is `procgroup_unix.go`/`procgroup_windows.go`, in this same package, for the same reason.

import "syscall"

func dieOfSignal(got syscall.Signal) {
	_ = syscall.Kill(syscall.Getpid(), got)
}
