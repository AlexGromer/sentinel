//go:build !windows

package svclog

// An advisory whole-file lock, so a purge and the services that are writing cannot lose each other's
// records (HEALTH-005 PR-B).
//
// Every writer here appends one complete line at a time, which is already atomic against other
// appenders. That is not the case a lock is for. The case is `agentctl purge-service`, which REWRITES
// the file: without a lock, a record appended between the purge reading the file and rewriting it is
// gone, silently, and the one operation that must never lose an audit record would be the one that
// loses them.
//
// Advisory rather than mandatory because that is what Linux offers, and it is enough: every process
// that rewrites this file is one of ours. A reader takes no lock at all — a torn last line is already
// tolerated by the scanner, and blocking reads behind a writer would make the journal's own endpoint
// the slowest thing in the process.

import (
	"os"
	"syscall"
)

// lockFile takes an exclusive advisory lock and returns the release. A lock that cannot be taken is
// NOT fatal and not reported: the fallback is exactly the behaviour this package had before the lock
// existed, and a service that refused to log because it could not lock would convert a logging
// problem into an outage — the same reasoning as Open returning nil rather than an error.
func lockFile(f *os.File) func() {
	if f == nil {
		return func() {}
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		return func() {}
	}
	return func() { _ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN) }
}

// Locked reports whether this build can protect a rewrite against concurrent appends. The purge
// command reads it so the limitation (see lock_windows.go) is announced by the tool rather than
// discovered by an operator.
const Locked = true
