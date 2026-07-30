package main

// ADR-107 completeness gate for the CLI projection: every route control-api serves is either reachable
// as an `agentctl` verb or carries a WRITTEN reason for not being.
//
// The gate reads the route registrations out of cmd/control-api/main.go rather than taking a list of
// routes on trust. That file's mux IS the authority on what the product serves, and a hand-kept copy of
// it would drift exactly the way the CLI drifted from HTTP in the first place — the M16 measurement
// found 16 capabilities with `cli = none`, none of which any test complained about.
//
// Reading a sibling package's source is the same technique the wizard drift gate uses on
// docs/setup/index.html, and for the same reason: the authority lives in a file, so the check goes to
// the file.

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

var routeRe = regexp.MustCompile(`(?m)^\s*m\.HandleFunc\("([A-Z]+) ([^"]+)"`)

// controlAPIRoutes returns every "METHOD /path" the mux registers.
func controlAPIRoutes(t *testing.T) []string {
	t.Helper()
	var out []string
	dir := filepath.Join("..", "control-api")
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read %s: %v", dir, err)
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".go") || strings.HasSuffix(e.Name(), "_test.go") {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			t.Fatalf("read %s: %v", e.Name(), err)
		}
		for _, m := range routeRe.FindAllStringSubmatch(string(raw), -1) {
			out = append(out, m[1]+" "+m[2])
		}
	}
	if len(out) < 20 {
		t.Fatalf("found only %d routes in cmd/control-api — the regex has stopped matching the "+
			"registrations, so this gate would pass by finding nothing: %v", len(out), out)
	}
	sort.Strings(out)
	return out
}

// normalisePath turns a mux pattern into the shape apiVerbs writes: {id} for any single wildcard.
func normalisePath(p string) string {
	return regexp.MustCompile(`\{[a-zA-Z_]+\}`).ReplaceAllString(p, "{id}")
}

// TestEveryControlAPIRouteHasACLIVerb is the gate. A new route with no verb and no recorded exemption
// fails it the moment it is registered.
func TestEveryControlAPIRouteHasACLIVerb(t *testing.T) {
	covered := map[string]string{} // "METHOD /path" -> verb
	for _, v := range apiVerbs {
		covered[v.Method+" "+normalisePath(v.Path)] = v.Verb
	}
	for _, route := range controlAPIRoutes(t) {
		parts := strings.SplitN(route, " ", 2)
		key := parts[0] + " " + normalisePath(parts[1])
		if _, ok := covered[key]; ok {
			continue
		}
		if reason, ok := apiRoutesWithoutCLI[route]; ok {
			if strings.TrimSpace(reason) == "" {
				t.Errorf("%s is exempted with an empty reason — an exemption with no reason is an omission "+
					"that learned to pass a test", route)
			}
			continue
		}
		t.Errorf("route %s has no agentctl verb and no recorded exemption — a capability reachable from "+
			"HTTP but not from a terminal is exactly what ADR-107 exists to stop", route)
	}
}

// TestNoStaleCLIExemptions: an exemption for a route that no longer exists is a claim about a surface
// that is gone, and it would keep a real gap quiet if the route came back under the same name.
func TestNoStaleCLIExemptions(t *testing.T) {
	live := map[string]bool{}
	for _, r := range controlAPIRoutes(t) {
		live[r] = true
	}
	for route := range apiRoutesWithoutCLI {
		if !live[route] {
			t.Errorf("apiRoutesWithoutCLI exempts %q, which control-api no longer registers", route)
		}
	}
}

// TestNoVerbPointsAtAMissingRoute: the reverse direction. A verb whose route does not exist sends a
// person to a 404 that reads like a broken server rather than a broken CLI.
func TestNoVerbPointsAtAMissingRoute(t *testing.T) {
	live := map[string]bool{}
	for _, r := range controlAPIRoutes(t) {
		parts := strings.SplitN(r, " ", 2)
		live[parts[0]+" "+normalisePath(parts[1])] = true
	}
	for _, v := range apiVerbs {
		if !live[v.Method+" "+normalisePath(v.Path)] {
			t.Errorf("verb %q targets %s %s, which control-api does not register", v.Verb, v.Method, v.Path)
		}
	}
}

// TestVerbsAreUnambiguous: two verbs sharing a prefix must still resolve, and none may be shadowed by a
// locally-implemented subcommand — a remote verb quietly answering for `run` would be a different tool
// wearing the same name.
func TestVerbsAreUnambiguous(t *testing.T) {
	local := map[string]bool{
		"run": true, "baseline": true, "locators": true, "revisions": true, "export-git": true,
		"export-spec": true, "import": true, "report": true, "calibrate": true, "redact-trace": true,
		"purge-store": true, "sweep-downloaded": true, "version": true,
	}
	seen := map[string]string{}
	for _, v := range apiVerbs {
		if prev, dup := seen[v.Verb]; dup {
			t.Errorf("verb %q is declared twice (%s and %s)", v.Verb, prev, v.Method+" "+v.Path)
		}
		seen[v.Verb] = v.Method + " " + v.Path

		head := strings.Fields(v.Verb)[0]
		if local[head] {
			t.Errorf("verb %q starts with %q, which is a locally-implemented subcommand — the dispatcher "+
				"matches local first, so this verb is unreachable", v.Verb, head)
		}
		// The longest-match resolver has to actually find it.
		got, rest := findAPIVerb(strings.Fields(v.Verb))
		if got == nil || got.Verb != v.Verb {
			name := "<nil>"
			if got != nil {
				name = got.Verb
			}
			t.Errorf("findAPIVerb(%q) resolved to %s, not itself", v.Verb, name)
		}
		if len(rest) != 0 {
			t.Errorf("findAPIVerb(%q) left %v unconsumed", v.Verb, rest)
		}
	}
}

// TestFindAPIVerbPrefersTheLongerPhrase: "config get" must not be swallowed by a shorter "config".
func TestFindAPIVerbPrefersTheLongerPhrase(t *testing.T) {
	v, rest := findAPIVerb([]string{"health", "live"})
	if v == nil || v.Verb != "health live" {
		t.Fatalf("health live resolved to %v", v)
	}
	if len(rest) != 0 {
		t.Fatalf("unconsumed: %v", rest)
	}
	// And the shorter one still resolves on its own, with the remainder handed back.
	v, rest = findAPIVerb([]string{"health"})
	if v == nil || v.Verb != "health" {
		t.Fatalf("health resolved to %v", v)
	}
	if len(rest) != 0 {
		t.Fatalf("unconsumed: %v", rest)
	}
	// An argument after a verb is left for the verb to consume, not treated as part of the phrase.
	v, rest = findAPIVerb([]string{"runs", "show", "abc123"})
	if v == nil || v.Verb != "runs show" {
		t.Fatalf("runs show resolved to %v", v)
	}
	if len(rest) != 1 || rest[0] != "abc123" {
		t.Fatalf("expected the id to survive as an argument, got %v", rest)
	}
	if v, _ := findAPIVerb([]string{"nonsense"}); v != nil {
		t.Errorf("an unknown word resolved to %q instead of nothing", v.Verb)
	}
}

// TestEveryVerbCarriesHelp: a verb nobody can discover is only nominally a projection.
func TestEveryVerbCarriesHelp(t *testing.T) {
	for _, v := range apiVerbs {
		if strings.TrimSpace(v.Help) == "" {
			t.Errorf("verb %q has no help text — it exists but cannot be found", v.Verb)
		}
		if v.Arg == "" && strings.Contains(v.Path, "{") {
			t.Errorf("verb %q targets a templated path %s but names no positional argument", v.Verb, v.Path)
		}
		if v.Arg != "" && !strings.Contains(v.Path, "{") {
			t.Errorf("verb %q names argument %q but its path %s has no placeholder", v.Verb, v.Arg, v.Path)
		}
	}
}
