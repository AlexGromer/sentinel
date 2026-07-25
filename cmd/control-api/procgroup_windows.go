//go:build windows

package main

// Windows has no process groups in the POSIX sense, so the tree is torn down with `taskkill /T`, which
// walks the parent/child chain the same way. This matters here and is not theoretical: the documented
// Windows path runs control-api natively while Ollama sits on the host, and a surviving Chromium would
// hold the run's trace file open — on Windows an open handle also blocks deleting the artifact directory,
// so an orphan is worse than a leak.
//
// `taskkill` is shipped with Windows and needs no privileges for a process the caller started.

import (
	"os/exec"
	"strconv"
)

// setProcGroup is a no-op: CREATE_NEW_PROCESS_GROUP would only affect Ctrl-C delivery, and the child
// chain here is torn down by pid instead.
func setProcGroup(*exec.Cmd) {}

// killProcTree terminates the process and its descendants. `hard` is accepted for signature parity with
// the Unix build: taskkill /F is already forceful, and there is no graceful step to skip — a Windows
// console app gets no SIGTERM equivalent it could act on here.
func killProcTree(pid int, hard bool) error {
	// /T walks the child tree, /F forces it. A non-zero exit usually means the tree was already gone,
	// which the caller treats as success — mirroring the ESRCH case on Unix.
	_ = hard
	return exec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(pid)).Run()
}
