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
	// maxConfigBytes bounds a PUT /v1/config body — the single source is configguard (also enforced at
	// the gateway, the real trust boundary, so a direct gRPC client cannot exceed it either).
	maxConfigBytes = configguard.MaxConfigBytes
)

type readyCheck struct {
	Status string `json:"status"`           // ok | skipped | error
	Detail string `json:"detail,omitempty"` // authenticated callers only
}

// readyState memoizes the last probe. The mutex is held ONLY to read/publish the memo and to claim the
// right to probe — NEVER across the outbound I/O itself. A single in-flight probe is coordinated by
// `inflight`+`done` (the single-flight), and `epoch` guards against a config write racing an in-flight
// probe: a PUT bumps `epoch`, and a probe that started before the bump refuses to publish its now-stale
// result, so `invalidateReadiness()` (called on the PUT path) can never be overwritten by an older probe.
type readyState struct {
	mu       sync.Mutex
	at       time.Time
	checks   map[string]readyCheck
	ready    bool
	epoch    uint64
	inflight bool
	done     chan struct{}
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

// readiness returns the memoized checks, refreshing them at most once per readyCacheTTL. The dependency
// I/O runs with NO lock held, so neither a concurrent /readyz poll nor a PUT /v1/config (which calls
// invalidateReadiness) is ever blocked behind a slow-but-live probe. It also never takes s.mu, so the
// run map stays serviceable throughout.
func (s *server) readiness() (map[string]readyCheck, bool) {
	s.ready.mu.Lock()
	for {
		if s.ready.checks != nil && time.Since(s.ready.at) < readyCacheTTL {
			c, r := s.ready.checks, s.ready.ready
			s.ready.mu.Unlock()
			return c, r
		}
		if s.ready.inflight { // another goroutine is probing; wait for it, then re-check the memo
			done := s.ready.done
			s.ready.mu.Unlock()
			<-done
			s.ready.mu.Lock()
			continue
		}
		break
	}
	// Claim the probe. epoch is snapshotted so a PUT that invalidates mid-probe is detected below.
	s.ready.inflight = true
	s.ready.done = make(chan struct{})
	done := s.ready.done
	epoch := s.ready.epoch
	s.ready.mu.Unlock()

	checks, ready := s.probeAll() // outbound I/O — NO lock held

	s.ready.mu.Lock()
	s.ready.inflight = false
	close(done)
	if s.ready.epoch == epoch { // no PUT raced us; publish. Otherwise drop — the next caller re-probes.
		s.ready.checks, s.ready.ready, s.ready.at = checks, ready, time.Now()
	}
	s.ready.mu.Unlock()
	return checks, ready
}

// probeAll runs the dependency probes with NO lock held (that is the whole point — see readiness). The
// probes are sequential because the llm base_url may come from the config document the store probe reads;
// the total time is bounded (ping + getConfig + llm, each <= readyProbeTimeout) and is absorbed by a
// single in-flight prober while others wait on the memo, so it never blocks a caller behind the lock.
func (s *server) probeAll() (map[string]readyCheck, bool) {
	checks := map[string]readyCheck{}
	var cfg map[string]any

	if s.store == nil {
		// ADR-075: the standalone tier really does keep the config in a file now, so the probe reads it.
		// A MISSING file stays "skipped" rather than "error": with no store configured, running purely
		// from the process env is a legitimate deployment, and flipping /readyz to 503 would call it
		// broken. The service tier treats `rec == nil` as an error for the opposite reason — pointing at
		// a gateway is an explicit declaration that a stored config is expected.
		if s.storeAddr != "" {
			checks["store"] = readyCheck{Status: "error", Detail: "store-gateway " + s.storeAddr + " did not answer at startup"}
			checks["config"] = readyCheck{Status: "error", Detail: storeUnavailableMsg}
		} else {
			checks["store"] = readyCheck{Status: "skipped", Detail: "CONTROL_API_STORE_ADDR unset (standalone tier)"}
			doc, ok, ferr := s.readConfigFile()
			switch {
			case ferr != nil:
				checks["config"] = readyCheck{Status: "error", Detail: ferr.Error()}
			case !ok:
				checks["config"] = readyCheck{Status: "skipped", Detail: "standalone tier: no config saved yet (the setup wizard writes " + s.configFilePath() + ")"}
			default:
				checks["config"] = readyCheck{Status: "ok", Detail: "standalone tier: " + s.configFilePath()}
				_ = json.Unmarshal(doc.ValueJson, &cfg) // best-effort: only used to find a base_url
			}
		}
	} else if err := s.store.ping(); err != nil {
		checks["store"] = readyCheck{Status: "error", Detail: err.Error()}
		checks["config"] = readyCheck{Status: "error", Detail: "store-gateway unreachable"}
	} else {
		checks["store"] = readyCheck{Status: "ok"}
		rec, err := s.store.getConfig(setupConfigKey, readyProbeTimeout)
		switch {
		case err != nil: // gateway hiccup, NOT "no config" — do not tell the operator to re-run the wizard
			checks["config"] = readyCheck{Status: "error", Detail: "store-gateway GetConfig failed: " + err.Error()}
		case rec == nil:
			checks["config"] = readyCheck{Status: "error", Detail: "no config stored; run the setup wizard"}
		default:
			checks["config"] = readyCheck{Status: "ok"}
			_ = json.Unmarshal([]byte(rec.ValueJson), &cfg) // best-effort: only used to find a base_url
		}
	}
	checks["llm"] = s.probeLLM(s.effectiveLLMBase(cfg))

	ready := true
	for _, c := range checks {
		if c.Status == "error" {
			ready = false
		}
	}
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
	// Shared shape check (validateLLMBase, llmenv.go / ADR-063): absolute http(s), no embedded
	// credential (which probing would send outbound), no link-local cloud-metadata target. Only a LITERAL
	// link-local IP is blocked; RFC1918 homelab hosts and loopback stay allowed (not DNS-rebinding-proof).
	if err := validateLLMBase(base); err != nil {
		return readyCheck{Status: "error", Detail: "base_url " + err.Error()}
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
	switch s.configTier() {
	case tierUnavailable:
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"error": storeUnavailableMsg, "tier": tierUnavailable})
		return
	case tierFile: // ADR-075: the standalone tier reads the document the wizard wrote
		doc, ok, ferr := s.readConfigFile()
		if ferr != nil { // corrupt/unreadable is NOT "no config" — see readConfigFile
			writeJSON(w, http.StatusInternalServerError, map[string]any{"error": ferr.Error(), "tier": tierFile})
			return
		}
		if !ok {
			writeJSON(w, http.StatusNotFound, map[string]any{"error": "no config stored", "tier": tierFile})
			return
		}
		var parsed any
		if err := json.Unmarshal(doc.ValueJson, &parsed); err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"error": "stored config is not valid JSON", "tier": tierFile})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"key": doc.Key, "updated_at": doc.UpdatedAt, "config": parsed, "tier": tierFile, "path": s.configFilePath()})
		return
	}
	rec, err := s.store.getConfig(setupConfigKey, storeCallTimeout)
	if err != nil { // a gateway failure is NOT a 404 — that would hide a real config behind a false miss
		writeJSON(w, http.StatusBadGateway, map[string]any{"error": "store-gateway unreachable"})
		return
	}
	if rec == nil {
		writeJSON(w, http.StatusNotFound, map[string]any{"error": "no config stored"})
		return
	}
	var doc any
	if err := json.Unmarshal([]byte(rec.ValueJson), &doc); err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]any{"error": "stored config is not valid JSON"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"key": rec.Key, "updated_at": rec.UpdatedAt, "config": doc, "tier": tierStore})
}

func (s *server) handlePutConfig(w http.ResponseWriter, r *http.Request) {
	// ADR-075: the tier decision comes AFTER validation, not before. The old order returned 501 without
	// ever reading the body, so a malformed document and a perfectly good one were refused identically —
	// and the refusal named a file tier that did not exist.
	if s.configTier() == tierUnavailable {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"error": storeUnavailableMsg, "tier": tierUnavailable})
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
	// ADR-065: the logging section names a closed vocabulary of levels and categories, so a typo is
	// refused HERE with the offending path rather than silently ignored at spawn time — a level that
	// looks saved but never applies is the same silent-degradation shape this milestone is closing.
	if err := validateLoggingSection(body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	if s.configTier() == tierFile { // ADR-075 standalone tier
		if err := s.writeConfigFile(string(body)); err != nil {
			// A config write that silently vanished would leave the operator believing the wizard had
			// saved — the same reason storeClient.putConfig is the one store helper that does not swallow
			// its error (cmd/control-api/store.go).
			writeJSON(w, http.StatusInternalServerError, map[string]any{
				"error": "cannot write the config file: " + err.Error(), "tier": tierFile, "path": s.configFilePath()})
			return
		}
		s.invalidateReadiness()
		writeJSON(w, http.StatusOK, map[string]any{
			"status": "saved", "key": setupConfigKey, "tier": tierFile, "path": s.configFilePath()})
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
	writeJSON(w, http.StatusOK, map[string]any{"status": "saved", "key": setupConfigKey, "tier": tierStore})
}

// invalidateReadiness drops the memo AND bumps epoch, so a probe that is already in flight (started
// before this write) will refuse to publish its now-stale result. It only ever holds s.ready.mu for
// these two field writes — never across I/O — so the PUT that calls it is not blocked behind a probe.
func (s *server) invalidateReadiness() {
	s.ready.mu.Lock()
	s.ready.checks, s.ready.at = nil, time.Time{}
	s.ready.epoch++
	s.ready.mu.Unlock()
}

// loadStartupConfig reads the persisted config once at boot (ADR-059 §7.4: "the control-API reads the
// config at start"). It is INFORMATIONAL — it logs what was found; readiness re-reads the document live
// on every probe, so nothing downstream depends on this completing. It therefore runs in the background:
// blocking ListenAndServe on a second store RPC (newStoreClient already spent up to its timeout dialing)
// could delay /healthz and /readyz past a tight k8s startupProbe. Startup ordering is not a correctness
// dependency here — the fail-open philosophy ("runs stay in-memory") extends to "start serving, let
// /readyz reflect eventual consistency".
//
// NOTE (ADR-063): the stored config's `llm` block IS now materialized into `agentctl run` spawns — it is
// the lowest-precedence layer in resolveRunEnv (process env > per-run > persisted; see getPersistedLLM,
// cmd/control-api/llmenv.go). This function stays informational (it only logs); readiness re-reads the
// document live, and the run path reads it per spawn. The REST of the persisted document (run/auth blocks)
// is still not fed into the run env.
func (s *server) loadStartupConfig() {
	switch s.configTier() {
	case tierUnavailable:
		fmt.Fprintf(os.Stderr, "control-api: WARNING — %s\n", storeUnavailableMsg)
		return
	case tierFile: // ADR-075
		doc, ok, ferr := s.readConfigFile()
		switch {
		case ferr != nil:
			fmt.Fprintf(os.Stderr, "control-api: WARNING — %v\n", ferr)
		case !ok:
			fmt.Fprintf(os.Stderr, "control-api: no persisted config yet (standalone tier; the setup wizard writes %s)\n", s.configFilePath())
		default:
			var fdoc map[string]any
			_ = json.Unmarshal(doc.ValueJson, &fdoc)
			if base := s.effectiveLLMBase(fdoc); base != "" && s.llmBaseURL == "" {
				fmt.Fprintf(os.Stderr, "control-api: config %q loaded from %s (llm.base_url=%s, updated_at=%s)\n", doc.Key, s.configFilePath(), base, doc.UpdatedAt)
			} else {
				fmt.Fprintf(os.Stderr, "control-api: config %q loaded from %s (updated_at=%s)\n", doc.Key, s.configFilePath(), doc.UpdatedAt)
			}
		}
		return
	}
	rec, err := s.store.getConfig(setupConfigKey, storeCallTimeout)
	if err != nil {
		fmt.Fprintf(os.Stderr, "control-api: startup config read failed (gateway): %v\n", err)
		return
	}
	if rec == nil {
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
