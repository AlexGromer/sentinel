//go:build windows

package svclog

// Windows has no flock. LockFileEx exists and would work, but reaching for it here would add a
// syscall surface for a case Windows does not have: the supported Windows deployment is the CLIENT
// (agentctl.exe against a remote control-api — see docs/DISTRIBUTION.md), so no two Sentinel
// processes share a journal file on a Windows host.
//
// The consequence is stated rather than hidden: on Windows a purge running CONCURRENTLY with a
// writing service can lose a record appended during the rewrite. `agentctl purge-service` says so
// when it runs there, and docs/OBSERVABILITY.md carries the same boundary.

import "os"

func lockFile(f *os.File) func() { return func() {} }

// Locked reports whether this build can protect a rewrite against concurrent appends. The purge
// command reads it so the limitation is announced by the tool rather than discovered by an operator.
const Locked = false
