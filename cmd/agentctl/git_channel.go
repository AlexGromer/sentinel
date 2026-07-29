package main

// PROD-IMPORT / PROD-EXPORT — git as an INPUT and as an OUTPUT (🟢OSS).
//
// The line here is protocol vs service, and it is deliberate. Reaching a repository over plain git
// (a local path, ssh, https) is plumbing: it is one more way to get at files, and the real work is
// done by the filesystem channel that already exists. Charging for it would paywall the on-ramp, and
// it would contradict the air-gapped promise the file-based revision store was justified by —
// an air-gapped customer usually has an internal GitLab, and "push to your own server, but pay us"
// is not a defensible line. Managed integration (stored credentials, PR/branch automation over the
// GitHub/GitLab/Bitbucket APIs, webhooks, conflict policy, multi-repo routing) is a SERVICE and is
// tracked separately, out of this repository.
//
// Two invariants that shape the code below:
//
//  1. A LOCAL PATH MUST WORK WITH NO NETWORK. That is what keeps the air-gapped install whole, and it
//     is also what lets the gate run against a bare repo in a temp dir rather than against the
//     internet — so CI and an air-gapped operator exercise the same path.
//  2. PUSH IS NEVER IMPLICIT. Writing into someone else's repository is an outward, irreversible act;
//     it happens only when asked for by name.

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// gitClone shallow-clones ref (or the default branch) of src into a fresh temp directory and returns
// it. `src` may be a URL or a local path — git treats both the same, which is exactly why the
// air-gapped case needs no separate code path.
func gitClone(src, ref string) (string, error) {
	if strings.TrimSpace(src) == "" {
		return "", fmt.Errorf("empty repository")
	}
	dir, err := os.MkdirTemp("", "sentinel-git-import-")
	if err != nil {
		return "", err
	}
	args := []string{"clone", "--depth", "1", "--no-tags"}
	if ref != "" {
		args = append(args, "--branch", ref)
	}
	// `--` before the source: a repository "URL" beginning with a dash would otherwise be read as a
	// git option. The input comes from a caller, so it is not ours to trust.
	args = append(args, "--", src, dir)
	cmd := exec.Command("git", args...)
	// Fail instead of blocking forever on a credential prompt: a CI run or an unattended import that
	// stops at an invisible password prompt looks like a hang, which is the least diagnosable failure.
	cmd.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0", "GIT_ASKPASS=", "GCM_INTERACTIVE=never")
	if out, err := cmd.CombinedOutput(); err != nil {
		os.RemoveAll(dir)
		return "", fmt.Errorf("git clone failed: %s", lastNonEmptyLine(string(out)))
	}
	// A clone can SUCCEED and check out nothing: a repository whose HEAD points at a branch that does
	// not exist (a bare repo initialised as `master` and pushed to as `main` — ordinary in the wild)
	// clones to an empty tree with only a warning. Downstream that surfaced as "no *.spec.ts found",
	// which sends the reader hunting for missing tests instead of a dangling HEAD. Measured on a real
	// bare repository, not imagined.
	entries, err := os.ReadDir(dir)
	if err != nil {
		os.RemoveAll(dir)
		return "", err
	}
	onlyGit := true
	for _, e := range entries {
		if e.Name() != ".git" {
			onlyGit = false
			break
		}
	}
	if onlyGit {
		os.RemoveAll(dir)
		return "", fmt.Errorf("clone of %s checked out nothing — its HEAD points at a branch that "+
			"does not exist (try --ref <branch>)", src)
	}
	return dir, nil
}

// gitCommitInto writes the given files into a working tree, commits them, and optionally pushes.
// Returns the commit sha, or an empty string when there was nothing to commit.
func gitCommitInto(worktree, subdir, message string, files map[string][]byte, push bool, branch string) (string, error) {
	if branch != "" {
		// -B: create or reset. An export that cannot state which branch it wrote to is not auditable.
		if out, err := run(worktree, "git", "checkout", "-B", branch); err != nil {
			return "", fmt.Errorf("git checkout %s: %s", branch, lastNonEmptyLine(out))
		}
	}
	target := worktree
	if subdir != "" {
		target = filepath.Join(worktree, subdir)
		if err := os.MkdirAll(target, 0o755); err != nil {
			return "", err
		}
	}
	for name, body := range files {
		// Only a base name is ever written — a generated spec name must not become a path.
		safe := filepath.Base(name)
		if safe == "" || safe == "." || safe == ".." {
			return "", fmt.Errorf("refusing to write %q", name)
		}
		if err := os.WriteFile(filepath.Join(target, safe), body, 0o644); err != nil {
			return "", err
		}
	}
	if out, err := run(worktree, "git", "add", "-A"); err != nil {
		return "", fmt.Errorf("git add: %s", lastNonEmptyLine(out))
	}
	// Nothing changed is a legitimate outcome, not an error: re-exporting an unchanged suite should
	// be a no-op rather than an empty commit, and the caller is told which it was.
	if out, _ := run(worktree, "git", "status", "--porcelain"); strings.TrimSpace(out) == "" {
		return "", nil
	}
	if out, err := run(worktree, "git", "commit", "-m", message); err != nil {
		return "", fmt.Errorf("git commit: %s", lastNonEmptyLine(out))
	}
	sha, _ := run(worktree, "git", "rev-parse", "HEAD")
	sha = strings.TrimSpace(sha)
	if push {
		args := []string{"push", "origin"}
		if branch != "" {
			args = append(args, branch)
		}
		c := exec.Command("git", args...)
		c.Dir = worktree
		c.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0")
		if out, err := c.CombinedOutput(); err != nil {
			// The commit stands even if the push fails; say both, so nobody assumes the work was lost.
			return sha, fmt.Errorf("committed %s but push failed: %s", sha[:8], lastNonEmptyLine(string(out)))
		}
	}
	return sha, nil
}

func run(dir, name string, args ...string) (string, error) {
	c := exec.Command(name, args...)
	c.Dir = dir
	c.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0")
	out, err := c.CombinedOutput()
	return string(out), err
}

func lastNonEmptyLine(s string) string {
	lines := strings.Split(strings.TrimRight(s, "\n"), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if strings.TrimSpace(lines[i]) != "" {
			l := strings.TrimSpace(lines[i])
			if len(l) > 300 {
				l = l[:300]
			}
			return l
		}
	}
	return ""
}

// isBareOrNotAWorktree reports whether path cannot be committed into directly — a bare repository, or
// something that is not a git repository at all. Both are cases where an in-place write is wrong.
func isBareOrNotAWorktree(path string) bool {
	out, err := run(path, "git", "rev-parse", "--is-bare-repository")
	if err != nil {
		return true // not a repository (or git refused) — treat as "cannot write in place"
	}
	return strings.TrimSpace(out) != "false"
}
