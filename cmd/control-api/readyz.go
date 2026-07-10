// M11.5 PR-5 (ADR-062): readiness + the service tier of the tiered config (ADR-049/ADR-059 §7).
//
// /healthz answers "this process is alive" and never touches a dependency. /readyz answers "this
// process can actually serve", and therefore probes the real ones:
//
//	store   — the store-gateway socket (skipped when CONTROL_API_STORE_ADDR is unset)
//	llm     — GET <base_url>/models        (skipped when no base_url is configured anywhere)
//	config  — a persisted config document  (skipped in the standalone/file tier, i.e. no store)
//
// A dependency that is NOT CONFIGURED is `skipped`, not failed: a default `docker compose up` — no
// store, no LLM_BASE_URL — is a legitimate, fully working standalone deployment, and a readiness probe
// that never went green there would be worse than useless in Kubernetes.
//
// /readyz is unauthenticated (like /healthz, so an orchestrator can poll it) and it makes an outbound
// request. Three consequences are designed for, not discovered later:
//
//  1. Results are cached for readyCacheTTL and probes are single-flighted, so hammering /readyz cannot
//     be turned into an amplifier against the LLM endpoint.
//  2. Probes carry a hard timeout, refuse redirects, and only speak http/https.
//  3. The `detail` strings (socket paths, internal hostnames, gateway errors) are returned ONLY to an
//     authenticated caller. Anonymous callers get the statuses. Otherwise /readyz is a free oracle for
//     the internal network.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"

	"github.com/AlexGromer/sentinel/internal/configguard"
)

// isInvalidArgument reports whether the gateway refused the document (a caller error, HTTP 400) as
// opposed to failing to store it (an infrastructure error, HTTP 502).
func isInvalidArgument(err error) bool {
	return grpcstatus.Code(err) == codes.InvalidArgument
}

const (
	// setupConfigKey is the single well-known document the wizard writes and the control-API reads.
	setupConfigKey = "setup"
	// readyProbeTimeout bounds each dependency probe (also used by storeClient.ping).
	readyProbeTimeout = 2 * time.Second
	// readyCacheTTL bounds how often an unauthenticated caller can drive a real probe.
	readyCacheTTL = 3 * time.Second
	// maxConfigBytes bounds a PUT /v1/config body. The document is a form, not a payload.
	maxConfigBytes = 64 << 10
)

type readyCheck struct {
	Status string `json:"status"`           // ok | skipped | error
	Detail string `json:"detail,omitempty"` // authenticated callers only
}

// readyState memoizes the last probe. Its mutex serializes probes (the single-flight): a burst of
// /readyz requests performs ONE round of dependency I/O, and the rest read the memo.
type readyState struct {
	mu     sync.Mutex
	at     time.Time
	checks map[string]readyCheck
	ready  bool
}

func (s *server) client() *http.Client {
	if s.httpClient != nil {
		return s.httpClient
	}
	return &http.Client{
		Timeout: readyProbeTimeout,
		// A readiness probe follows nothing. A 302 to a different host would turn /readyz into a
		// request forwarder aimed wherever the redirect points.
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

// readiness returns the memoized checks, refreshing them at most once per readyCacheTTL.
// It never takes s.mu: the run map must stay serviceable while a dependency probe is in flight.
func (s *server) readiness() (map[string]readyCheck, bool) {
	s.ready.mu.Lock()
	defer s.ready.mu.Unlock()
	if s.ready.checks != nil && time.Since(s.ready.at) < readyCacheTTL {
		return s.ready.checks, s.ready.ready
	}

	checks := map[string]readyCheck{}
	var cfg map[string]any

	switch {
	case s.store == nil:
		checks["store"] = readyCheck{Status: "skipped", Detail: "CONTROL_API_STORE_ADDR unset (standalone tier)"}
		checks["config"] = readyCheck{Status: "skipped", Detail: "standalone tier: config is a file (brain/runconfig.py)"}
	default:
		if err := s.store.ping(); err != nil {
			checks["store"] = readyCheck{Status: "error", Detail: err.Error()}
			checks["config"] = readyCheck{Status: "error", Detail: "store-gateway unreachable"}
		} else {
			checks["store"] = readyCheck{Status: "ok"}
			rec, ok := s.store.getConfig(setupConfigKey)
			if !ok {
				checks["config"] = readyCheck{Status: "error", Detail: "no config stored; run the setup wizard"}
			} else {
				checks["config"] = readyCheck{Status: "ok"}
				_ = json.Unmarshal([]byte(rec.ValueJson), &cfg) // best-effort: only used to find a base_url
			}
		}
	}
	checks["llm"] = s.probeLLM(s.effectiveLLMBase(cfg))

	ready := true
	for _, c := range checks {
		if c.Status == "error" {
			ready = false
		}
	}
	s.ready.checks, s.ready.ready, s.ready.at = checks, ready, time.Now()
	return checks, ready
}

// effectiveLLMBase prefers the process env (the operator's explicit choice) over the persisted config.
func (s *server) effectiveLLMBase(cfg map[string]any) string {
	if s.llmBaseURL != "" {
		return s.llmBaseURL
	}
	llm, ok := cfg["llm"].(map[string]any)
	if !ok {
		return ""
	}
	base, _ := llm["base_url"].(string)
	return base
}

// probeLLM issues GET <base>/models — the OpenAI-compatible liveness surface every runtime preset in
// docs/backend-presets.json serves. base_url already carries the /v1 suffix (see the presets).
func (s *server) probeLLM(base string) readyCheck {
	base = strings.TrimRight(strings.TrimSpace(base), "/")
	if base == "" {
		return readyCheck{Status: "skipped", Detail: "no LLM_BASE_URL and no llm.base_url in the config (anthropic native, or offline heuristic)"}
	}
	u, err := url.Parse(base)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return readyCheck{Status: "error", Detail: fmt.Sprintf("base_url must be an absolute http(s) URL, got %q", base)}
	}
	resp, err := s.client().Get(base + "/models")
	if err != nil {
		return readyCheck{Status: "error", Detail: err.Error()}
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4<<10)) // drain a little so the conn can be reused
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return readyCheck{Status: "error", Detail: fmt.Sprintf("GET %s/models -> HTTP %d", base, resp.StatusCode)}
	}
	return readyCheck{Status: "ok"}
}

// handleReadyz is k8s-shaped: 200 when every configured dependency answers, 503 until then.
func (s *server) handleReadyz(w http.ResponseWriter, r *http.Request) {
	checks, ready := s.readiness()
	authed := s.authed(r)
	out := make(map[string]readyCheck, len(checks))
	for name, c := range checks {
		if !authed {
			c.Detail = "" // an anonymous caller learns the verdict, never the topology
		}
		out[name] = c
	}
	verdict, code := "not_ready", http.StatusServiceUnavailable
	if ready {
		verdict, code = "ready", http.StatusOK
	}
	writeJSON(w, code, map[string]any{"status": verdict, "version": version, "checks": out})
}

// --- GET/PUT /v1/config -------------------------------------------------------------------------
// Both are token-gated. The document holds no secrets (the gateway refuses them), but it does name
// internal hosts and models — deployment shape an anonymous caller has no business reading.

func (s *server) handleGetConfig(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]any{"error": "forbidden"})
		return
	}
	if s.store == nil {
		writeJSON(w, http.StatusNotImplemented, map[string]any{
			"error": "no store-gateway configured; this deployment keeps its config in a file (standalone tier)"})
		return
	}
	rec, ok := s.store.getConfig(setupConfigKey)
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]any{"error": "no config stored"})
		return
	}
	var doc any
	if err := json.Unmarshal([]byte(rec.ValueJson), &doc); err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]any{"error": "stored config is not valid JSON"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"key": rec.Key, "updated_at": rec.UpdatedAt, "config": doc})
}

func (s *server) handlePutConfig(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]any{"error": "forbidden"})
		return
	}
	if s.store == nil {
		writeJSON(w, http.StatusNotImplemented, map[string]any{
			"error": "no store-gateway configured; this deployment keeps its config in a file (standalone tier)"})
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxConfigBytes+1))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "unreadable body"})
		return
	}
	if len(body) > maxConfigBytes {
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]any{"error": "config document too large"})
		return
	}
	// Applied here so the caller sees WHICH member was refused; the gateway enforces the same rule
	// (same package) for callers that never pass through this process.
	if err := configguard.Validate(string(body)); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	if err := s.store.putConfig(setupConfigKey, string(body)); err != nil {
		if isInvalidArgument(err) {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusBadGateway, map[string]any{"error": "store-gateway rejected the write"})
		return
	}
	s.invalidateReadiness() // the "config" check just changed; do not serve a stale 503 for 3 more seconds
	writeJSON(w, http.StatusOK, map[string]any{"status": "saved", "key": setupConfigKey})
}

func (s *server) invalidateReadiness() {
	s.ready.mu.Lock()
	s.ready.checks, s.ready.at = nil, time.Time{}
	s.ready.mu.Unlock()
}

// loadStartupConfig reads the persisted config once at boot (ADR-059 §7.4: "control-API reads the
// config at start"). Bounded by the store client's own timeout, so an unreachable gateway delays the
// listener by at most that, never indefinitely.
func (s *server) loadStartupConfig() {
	if s.store == nil {
		return
	}
	rec, ok := s.store.getConfig(setupConfigKey)
	if !ok {
		fmt.Fprintln(os.Stderr, "control-api: no persisted config (key \"setup\"); /readyz stays 503 until the wizard saves one")
		return
	}
	var doc map[string]any
	if err := json.Unmarshal([]byte(rec.ValueJson), &doc); err != nil {
		fmt.Fprintf(os.Stderr, "control-api: WARNING — persisted config is not valid JSON: %v\n", err)
		return
	}
	if base := s.effectiveLLMBase(doc); base != "" && s.llmBaseURL == "" {
		fmt.Fprintf(os.Stderr, "control-api: config %q loaded (llm.base_url=%s, updated_at=%s)\n", rec.Key, base, rec.UpdatedAt)
		return
	}
	fmt.Fprintf(os.Stderr, "control-api: config %q loaded (updated_at=%s)\n", rec.Key, rec.UpdatedAt)
}
