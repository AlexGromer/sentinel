//go:build windows

package main

// dieOfSignal — the Windows half. See reraise_unix.go for why this is split at all.
//
// ⚠ THE STATUS IS NOT THE SAME, AND SAYING SO IS THE POINT. Windows has no `kill(2)` and no
// "terminated by signal" exit status: a process cannot re-raise SIGTERM at itself, and the
// 128+signal convention a Unix supervisor reads does not exist here. So this exits with 1, and a
// Windows service manager sees a plain non-zero exit rather than a signalled death.
//
// The alternative — exiting 0 to look "clean" — would be worse, and this is the shape of decision
// this repository keeps making the same way: a service that was shot and reports success is
// indistinguishable from one that finished its work, and that is the distinction a supervisor's
// restart policy turns on. The obituary in the journal carries the real reason ("signal SIGTERM"),
// so the fact is recorded where it can be read; only the exit code cannot express it.
//
// `os.Exit` rather than returning: the caller is a goroutine, and the whole purpose of this call is
// that the process must not continue past it.

import (
	"os"
	"syscall"
)

func dieOfSignal(_ syscall.Signal) {
	os.Exit(1)
}
