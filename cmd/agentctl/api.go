package main

// ADR-107, the CLI projection. `agentctl` was a run-and-artifact tool: everything about the STORE and
// the CONFIG existed on HTTP alone. The M16 measurement found 16 capabilities reachable only from the
// UI, and every one of them had `cli = none` — listing or deleting a scenario, promoting a test,
// reading results or trends, filtering a run's logs, reading or writing the config, asking whether the
// service is ready. A person who prefers a terminal simply could not do half the product.
//
// These are THIN clients over the routes control-api already serves. Not one of them reimplements a
// behaviour: a second implementation is how two surfaces come to disagree about what a capability
// means, which is the defect this whole milestone exists to remove.
//
// One table, `apiVerbs`, is both the implementation and the thing the completeness gate walks
// (api_projection_test.go). It cannot drift from what the CLI does, because it IS what the CLI does.

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// apiVerb projects one control-api route onto a CLI verb.
type apiVerb struct {
	Verb   string   // words the user types, e.g. "scenarios list"
	Method string   // HTTP method
	Path   string   // route, with {id} where a positional argument goes
	Arg    string   // name of the positional argument filling {id} ("" when the path has none)
	Query  []string // flags forwarded as query parameters
	Body   []string // flags sent as a JSON object (POST/PUT)
	Stdin  bool     // read the request body from a file/stdin instead of flags (config set)
	// SecretField names a body field whose value is read from STDIN via --<field>-stdin, never from a
	// flag. A password on an argv is visible to every `ps` on the host and lands in shell history; a
	// convenience worth exactly nothing against that.
	SecretField string
	Stream      bool // copy the response through instead of parsing it (SSE)
	Help        string
}

// apiVerbs is the CLI half of the one configuration/data model. Ordered as a person would look for
// things, not as the mux registers them.
var apiVerbs = []apiVerb{
	{Verb: "health", Method: "GET", Path: "/readyz", Help: "readiness: store, LLM endpoint, config"},
	{Verb: "health live", Method: "GET", Path: "/healthz", Help: "liveness only (no dependency probes)"},
	// QA-REPORT-SERVICE (ADR-119). A Stream verb: the body is Prometheus text, so it is copied through
	// untouched and redirected to a file — `agentctl metrics > scrape.prom`. The machine token this CLI
	// already holds is the unscoped credential, which is the one an operator scraping a deployment
	// wants; a session sees its own runs.
	{Verb: "metrics", Method: "GET", Path: "/metrics", Stream: true, Help: "aggregate Prometheus scrape over this deployment's runs, to stdout"},

	// ADR-111 — the live view from a terminal. `frame` and `stream` are Stream verbs: the response is
	// JPEG (or a multipart of JPEGs), so it is copied through untouched and redirected to a file.
	// A capability that exists in the UI and not here is the gap ADR-107 exists to close, and a
	// screenshot of what the browser is doing is exactly what a CI job or a remote operator wants
	// without opening a browser to look at a browser.
	//   agentctl live frame > shot.jpg
	// LIVE-PER-RUN (ADR-121): --run-id names WHICH run's picture. Omitting it stays legal and answers
	// about the newest page, which the service marks `scoped:false` — the terminal deserves the same
	// distinction the hub has, or `agentctl live frame` during two runs is a coin toss.
	{Verb: "live status", Method: "GET", Path: "/v1/live/status", Query: []string{"run_id"}, Help: "is a live view available, and is a page open (--run-id scopes it to one run)"},
	{Verb: "live frame", Method: "GET", Path: "/v1/live/frame.jpg", Query: []string{"run_id"}, Stream: true, Help: "one JPEG of what the browser sees, to stdout (--run-id scopes it)"},
	{Verb: "live stream", Method: "GET", Path: "/v1/live/mjpeg", Query: []string{"run_id"}, Stream: true, Help: "the live screencast as multipart JPEG, to stdout (--run-id scopes it; Ctrl-C to stop)"},

	{Verb: "config schema", Method: "GET", Path: "/v1/config-schema", Help: "every knob the product has, with its env name and default"},
	{Verb: "config get", Method: "GET", Path: "/v1/config", Help: "the persisted config document"},
	{Verb: "config set", Method: "PUT", Path: "/v1/config", Stdin: true, Help: "replace the persisted config from a JSON file (--file, or - for stdin)"},

	{Verb: "runs list", Method: "GET", Path: "/v1/runs", Query: []string{"limit", "offset"}, Help: "runs, newest first"},
	{Verb: "runs show", Method: "GET", Path: "/v1/runs/{id}", Arg: "run_id", Help: "one run's record"},
	{Verb: "runs cancel", Method: "POST", Path: "/v1/runs/{id}/cancel", Arg: "run_id", Help: "stop a running run"},
	{Verb: "runs events", Method: "GET", Path: "/v1/runs/{id}/events", Arg: "run_id", Stream: true, Help: "follow a run's event stream (SSE) until it ends"},
	{Verb: "runs artifact", Method: "GET", Path: "/v1/runs/{id}/artifact", Arg: "run_id", Query: []string{"name"}, Stream: true, Help: "fetch one whitelisted artifact to stdout (--name plan.json)"},

	{Verb: "logs", Method: "GET", Path: "/v1/runs/{id}/logs", Arg: "run_id",
		Query: []string{"lvl", "cat", "mod", "code", "src", "step", "q", "after", "limit"},
		Help:  "a run's structured diagnostics; --src takes a source OR an audience name (business|tool)"},
	// HEALTH-005 PR-B. The OTHER stream: `logs` is about one run, this is about the tool itself —
	// sign-ins and failed sign-ins, accounts created and deleted, configuration changes, refusals,
	// service start and stop. A regular account sees the events it owns; an admin or the machine token
	// sees the deployment's, which is why this is worth having from a terminal at all: the machine
	// token is what a CI job and a remote operator already hold.
	{Verb: "service-log", Method: "GET", Path: "/v1/service-log",
		Query: []string{"lvl", "code", "svc", "actor", "q", "limit"},
		Help:  "the service journal: sign-ins, account/config changes, refusals, service start/stop (newest last)"},
	{Verb: "events-catalog", Method: "GET", Path: "/v1/events-catalog", Help: "every event the brain can emit, with its bilingual phrasing"},

	{Verb: "scenarios list", Method: "GET", Path: "/v1/scenarios", Query: []string{"limit", "offset"}, Help: "saved scenarios"},
	{Verb: "scenarios show", Method: "GET", Path: "/v1/scenarios/{id}", Arg: "scenario_id", Help: "one scenario"},
	{Verb: "scenarios delete", Method: "DELETE", Path: "/v1/scenarios/{id}", Arg: "scenario_id", Help: "delete a scenario"},

	{Verb: "tests list", Method: "GET", Path: "/v1/tests", Query: []string{"limit", "offset"}, Help: "promoted tests"},
	{Verb: "tests show", Method: "GET", Path: "/v1/tests/{id}", Arg: "test_id", Help: "one test"},
	{Verb: "tests promote", Method: "POST", Path: "/v1/tests/promote", Body: []string{"scenario_id", "name"}, Help: "promote a scenario into a durable named test"},
	{Verb: "tests delete", Method: "DELETE", Path: "/v1/tests/{id}", Arg: "test_id", Help: "delete a test"},

	{Verb: "chats list", Method: "GET", Path: "/v1/chats", Query: []string{"limit", "offset"}, Help: "conversations"},
	{Verb: "chats show", Method: "GET", Path: "/v1/chats/{id}", Arg: "conversation_id", Help: "one conversation's projection"},
	{Verb: "chats delete", Method: "DELETE", Path: "/v1/chats/{id}", Arg: "conversation_id", Help: "delete a conversation's index row (the thread itself is untouched)"},

	{Verb: "results list", Method: "GET", Path: "/v1/results", Query: []string{"limit", "offset"}, Help: "run verdicts"},
	{Verb: "results show", Method: "GET", Path: "/v1/results/{id}", Arg: "run_id", Help: "one result record"},
	{Verb: "trends", Method: "GET", Path: "/v1/trends", Query: []string{"metric", "window"}, Help: "a metric over time (--metric coverage --window 50)"},

	// ADR-109 local accounts.
	{Verb: "whoami", Method: "GET", Path: "/v1/me", Help: "which credential this is and whether it is scoped"},
	{Verb: "users list", Method: "GET", Path: "/v1/users", Help: "local accounts (machine token or an admin)"},
	{Verb: "users add", Method: "POST", Path: "/v1/users", Body: []string{"name", "is_admin"},
		SecretField: "password",
		Help:        "create a local account; the password is read from stdin via --password-stdin"},
	{Verb: "users remove", Method: "DELETE", Path: "/v1/users/{id}", Arg: "user_id", Help: "remove a local account (its rows stay, unowned)"},
}

// apiRoutesWithoutCLI names routes that deliberately have no CLI verb, each with the reason. The
// completeness gate reads this list, so an exemption is a recorded decision rather than an omission
// nobody noticed.
var apiRoutesWithoutCLI = map[string]string{
	"POST /v1/runs":             "`agentctl run` IS this route's local equivalent — it spawns the same brain directly, without a server",
	"POST /v1/chat/completions": "the OpenAI-compat shim exists for foreign clients; `agentctl run --mode chat --conversation-id` is the local form",
	"POST /v1/import":           "`agentctl import` IS the implementation — the route spawns this binary, so a CLI verb would call itself",
	"GET /v1/stream":            "a WebSocket with a takeover/return control channel; `runs events` covers the read side over SSE, and driving a takeover from a terminal has no meaning",
	"GET /v1/ui-token":          "hands a browser tab its token during bootstrap; a CLI already has the token it would be asking for",
	"POST /v1/login":            "a CLI already holds the machine token, which is strictly more powerful than any session, so logging in from a terminal buys nothing — and would put a password on an argv that every `ps` on the host can read",
	"POST /v1/logout":           "there is no CLI session to end (see POST /v1/login)",
	"GET /v1/":                  "the catch-all 404 for unknown /v1 paths, not a capability",
	"GET /v1/live/screen":       "a WebSocket carrying RFB — PIXELS, not text. There is nothing for a terminal to render, and a verb that dumped a framebuffer would be a file nobody asked for; `agentctl live status` answers what a CLI can actually use (whether there IS a screen, why not, and at which in-network address)",

	// The four revision routes ARE reachable from a terminal: `agentctl revisions list|show|diff|rollback`
	// (cmdRevisions) predates them and reads the store DIRECTLY rather than through a server. That is the
	// better local form — a person inspecting a test's history on the machine that holds it should not
	// need control-api running. Adding table verbs beside them would give one capability two spellings
	// with different prerequisites, which is how two surfaces begin to disagree.
	"GET /v1/tests/{id}/revisions":           "local `agentctl revisions list` reads the store directly — no server required",
	"GET /v1/tests/{id}/revisions/show":      "local `agentctl revisions show`",
	"GET /v1/tests/{id}/revisions/diff":      "local `agentctl revisions diff`",
	"POST /v1/tests/{id}/revisions/rollback": "local `agentctl revisions rollback`",
}

// controlAPIBase resolves the control-api this CLI talks to.
func controlAPIBase() string {
	if v := strings.TrimRight(os.Getenv("CONTROL_API_URL"), "/"); v != "" {
		return v
	}
	return "http://127.0.0.1:8090"
}

// controlAPIToken finds the bearer token: the environment first, then the file control-api persists
// (ADR-064). Reading the file is what makes the CLI usable against a locally started server without
// the person having to copy a token out of a log line.
func controlAPIToken(repo string) string {
	if v := os.Getenv("CONTROL_API_TOKEN"); v != "" {
		return v
	}
	if b, err := os.ReadFile(filepath.Join(repo, "state", "control-api.token")); err == nil {
		return strings.TrimSpace(string(b))
	}
	return ""
}

// findAPIVerb matches the longest verb phrase against the leading arguments, so "config get" wins over
// a hypothetical "config". Returns the verb and the arguments left after it.
func findAPIVerb(args []string) (*apiVerb, []string) {
	best := -1
	bestWords := 0
	for i := range apiVerbs {
		w := strings.Fields(apiVerbs[i].Verb)
		if len(w) > len(args) || len(w) <= bestWords {
			continue
		}
		match := true
		for j, word := range w {
			if args[j] != word {
				match = false
				break
			}
		}
		if match {
			best, bestWords = i, len(w)
		}
	}
	if best < 0 {
		return nil, nil
	}
	return &apiVerbs[best], args[bestWords:]
}

// apiUsage prints every verb, grouped by its first word.
func apiUsage(w io.Writer) {
	fmt.Fprintln(w, "control-api verbs (set CONTROL_API_URL / CONTROL_API_TOKEN, or run against a local server):")
	var last string
	for _, v := range apiVerbs {
		head := strings.Fields(v.Verb)[0]
		if head != last {
			fmt.Fprintln(w)
			last = head
		}
		arg := ""
		if v.Arg != "" {
			arg = " <" + v.Arg + ">"
		}
		flags := ""
		for _, q := range v.Query {
			flags += " [--" + q + " …]"
		}
		for _, b := range v.Body {
			flags += " --" + b + " …"
		}
		if v.Stdin {
			flags += " --file <f>|-"
		}
		if v.SecretField != "" {
			flags += " --" + v.SecretField + "-stdin"
		}
		fmt.Fprintf(w, "  agentctl %s%s%s\n      %s\n", v.Verb, arg, flags, v.Help)
	}
}

// cmdAPI runs one apiVerb. Flags are parsed by hand rather than through flag.FlagSet because the
// accepted set is data (v.Query / v.Body), not a compile-time list — the same reason the completeness
// gate can walk it.
func cmdAPI(repo string, v *apiVerb, rest []string) int {
	path := v.Path
	if v.Arg != "" {
		if len(rest) == 0 || strings.HasPrefix(rest[0], "-") {
			fmt.Fprintf(os.Stderr, "error: %s needs a <%s>\n", v.Verb, v.Arg)
			return 2
		}
		path = strings.Replace(path, "{id}", url.PathEscape(rest[0]), 1)
		rest = rest[1:]
	}

	allowed := map[string]bool{}
	for _, q := range v.Query {
		allowed[q] = true
	}
	for _, b := range v.Body {
		allowed[b] = true
	}
	if v.Stdin {
		allowed["file"] = true
	}
	if v.SecretField != "" {
		allowed[v.SecretField+"-stdin"] = true
	}

	vals := map[string]string{}
	for i := 0; i < len(rest); i++ {
		a := rest[i]
		if !strings.HasPrefix(a, "--") {
			fmt.Fprintf(os.Stderr, "error: unexpected argument %q for %s\n", a, v.Verb)
			return 2
		}
		name, val := strings.TrimPrefix(a, "--"), ""
		if eq := strings.IndexByte(name, '='); eq >= 0 {
			name, val = name[:eq], name[eq+1:]
		} else if v.SecretField != "" && name == v.SecretField+"-stdin" {
			// A valueless switch: it says WHERE the secret comes from, not what it is. Demanding a value
			// here is how the flag whose whole purpose is to keep a password off the argv ended up asking
			// for one on the argv.
			val = "yes"
		} else {
			if i+1 >= len(rest) {
				fmt.Fprintf(os.Stderr, "error: --%s needs a value\n", name)
				return 2
			}
			i++
			val = rest[i]
		}
		if !allowed[name] {
			// Naming what IS accepted, because a rejected flag with no alternatives listed sends the
			// reader to the source of a CLI they were hoping not to read.
			var ok []string
			for k := range allowed {
				ok = append(ok, "--"+k)
			}
			fmt.Fprintf(os.Stderr, "error: %s does not accept --%s (accepts: %s)\n", v.Verb, name, strings.Join(ok, " "))
			return 2
		}
		vals[name] = val
	}

	q := url.Values{}
	for _, k := range v.Query {
		if val, ok := vals[k]; ok {
			q.Set(k, val)
		}
	}
	if len(q) > 0 {
		path += "?" + q.Encode()
	}

	var body io.Reader
	if len(v.Body) > 0 || v.SecretField != "" {
		obj := map[string]any{}
		for _, k := range v.Body {
			if val, ok := vals[k]; ok {
				// "true"/"false" become JSON booleans. A server field declared bool would otherwise reject
				// the string "true" — and a CLI that can only ever send strings could not set one at all.
				switch val {
				case "true":
					obj[k] = true
				case "false":
					obj[k] = false
				default:
					obj[k] = val
				}
			}
		}
		if v.SecretField != "" {
			if _, asked := vals[v.SecretField+"-stdin"]; asked {
				secret, err := io.ReadAll(os.Stdin)
				if err != nil {
					fmt.Fprintf(os.Stderr, "error: reading %s from stdin: %v\n", v.SecretField, err)
					return 2
				}
				// One trailing newline is what `echo` and a here-string add; anything else the person typed
				// is theirs to keep, because a password may legitimately end in a space.
				obj[v.SecretField] = strings.TrimRight(string(secret), "\r\n")
			} else {
				fmt.Fprintf(os.Stderr, "error: %s needs --%s-stdin (the %s is read from stdin, never from an "+
					"argv every `ps` on the host can read)\n", v.Verb, v.SecretField, v.SecretField)
				return 2
			}
		}
		raw, _ := json.Marshal(obj)
		body = strings.NewReader(string(raw))
	}
	if v.Stdin {
		src := vals["file"]
		if src == "" {
			fmt.Fprintf(os.Stderr, "error: %s needs --file <path> (or --file - to read stdin)\n", v.Verb)
			return 2
		}
		var raw []byte
		var err error
		if src == "-" {
			raw, err = io.ReadAll(os.Stdin)
		} else {
			raw, err = os.ReadFile(src)
		}
		if err != nil {
			fmt.Fprintf(os.Stderr, "error: read %s: %v\n", src, err)
			return 2
		}
		// Parsed before sending so a typo fails here, naming the offset, instead of arriving at the
		// server as a 400 whose message is about the wire format rather than the file.
		if !json.Valid(raw) {
			fmt.Fprintf(os.Stderr, "error: %s is not valid JSON\n", src)
			return 2
		}
		body = strings.NewReader(string(raw))
	}

	req, err := http.NewRequest(v.Method, controlAPIBase()+path, body)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 2
	}
	if tok := controlAPIToken(repo); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	// No timeout on a stream: `runs events` follows a run to its end, which is as long as the run.
	client := &http.Client{}
	if !v.Stream {
		client.Timeout = 30 * time.Second
	}
	resp, err := client.Do(req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %s %s: %v\n  (control-api at %s — set CONTROL_API_URL if it listens elsewhere)\n",
			v.Method, path, err, controlAPIBase())
		return 4
	}
	defer resp.Body.Close()

	if v.Stream && resp.StatusCode == http.StatusOK {
		if _, err := io.Copy(os.Stdout, resp.Body); err != nil {
			fmt.Fprintf(os.Stderr, "error: stream: %v\n", err)
			return 4
		}
		return 0
	}

	raw, _ := io.ReadAll(resp.Body)

	// A FAILED stream verb writes its body to STDERR, never stdout. `agentctl live frame > shot.jpg`
	// is the intended use, and on a 503 the old path put the server's explanation INTO shot.jpg —
	// producing a file that looks like a corrupt image and hides a message that was perfectly clear.
	// Non-stream verbs keep printing to stdout: their output is text a person is reading, and a
	// pipeline consuming it wants the error where the data would have been.
	if v.Stream {
		if len(raw) > 0 {
			fmt.Fprintf(os.Stderr, "%s\n", strings.TrimRight(string(raw), "\n"))
		}
		fmt.Fprintf(os.Stderr, "error: %s %s -> %d\n", v.Method, path, resp.StatusCode)
		return 4
	}

	// Re-indent JSON so a person reading a terminal sees structure; anything else is passed through
	// byte-for-byte rather than mangled into a quoted string.
	var pretty any
	if json.Unmarshal(raw, &pretty) == nil {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(pretty)
	} else if len(raw) > 0 {
		os.Stdout.Write(raw)
		if raw[len(raw)-1] != '\n' {
			fmt.Println()
		}
	}
	if resp.StatusCode >= 400 {
		fmt.Fprintf(os.Stderr, "error: %s %s -> %d\n", v.Method, v.Path, resp.StatusCode)
		return 1
	}
	return 0
}
