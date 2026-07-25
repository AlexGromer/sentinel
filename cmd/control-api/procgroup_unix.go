//go:build !windows

package main

// Killing a run means killing a TREE, not a process (M9-LIVE). control-api spawns agentctl, which
// spawns the Python brain, which spawns the Playwright executor, which spawns Chromium. Signalling only
// the top of that chain leaves a browser running and a trace file open — the orphan then holds the run's
// artifacts and, on a repeat, its port.
//
// On Unix the whole chain is put into its own process group at spawn time, so one signal reaches all of
// it. The group is created deliberately rather than inherited: control-api's own group would otherwise
// receive the signal too.

import (
	"os/exec"
	"syscall"
)

// setProcGroup makes the spawned command the leader of a fresh process group.
func setProcGroup(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
}

// killProcTree signals the whole group. A negative pid means "the group led by pid" — the same
// convention `kill -TERM -1234` uses.
//
// SIGTERM first so the executor can close its trace and the brain can write what it has; the caller
// escalates to killProcTreeHard if the group is still alive after a grace period. Errors are returned
// rather than logged so the handler can tell an already-dead run from a failure to signal.
func killProcTree(pid int, hard bool) error {
	sig := syscall.SIGTERM
	if hard {
		sig = syscall.SIGKILL
	}
	if err := syscall.Kill(-pid, sig); err != nil {
		// The group may already be gone (the run finished between the check and the signal), which is a
		// success from the caller's point of view.
		if err == syscall.ESRCH {
			return nil
		}
		return err
	}
	return nil
}
