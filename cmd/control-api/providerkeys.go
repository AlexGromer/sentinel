// Provider keys as SETTINGS (ADR-146, Alex's directives 2026-08-07 and 2026-08-09).
//
// Until this file the product's posture was the opposite one, and it was written down as an invariant
// in llmenv.go: "an api_key never travels through the API or UI". That was not an oversight — it is
// what configguard enforces at two tiers, and the reasoning is sound for the CONFIG domain. What it
// left, though, was a product with no way to give the tool a key at all: the setup wizard renders
// `export LLM_API_KEY=...` as a PLACEHOLDER for the operator to fill in a shell, and docs/index.html
// says so in as many words — "No api_key — secrets stay in the server env". A tool that cannot be
// told its own key is a tool configured by editing a compose file.
//
// The directive reverses the posture, and in a specific shape:
//
//	"Keys are configured ONCE in settings, like the backend, and live until an administrator
//	 replaces them."   -> there is no per-run key field, and llmRunConfig keeps having no api_key
//	                      member. A key is not a property of a run.
//	"BOTH storage paths must exist at once."
//	                   -> environment passthrough is NOT replaced. It stays, and it keeps WINNING,
//	                      because air-gapped deployments, CI, and anyone holding credentials in an
//	                      external store depend on it.
//
// WHY A FILE, AND NEVER THE STORE. This is the one decision here that is not mechanical, so the
// reason is recorded rather than implied. The store-gateway's gRPC socket is reachable by any
// same-UID process holding STORE_TOKEN — configguard's own package comment gives that as the reason
// the guard exists. Persisting a credential behind that boundary would DEFEAT the guard rather than
// relax it. A 0600 file under state/ is the tighter container: the medium the standalone config tier
// already uses (ADR-075), the same atomic temp+rename write, and no second reader.
//
// Consequently configguard is NOT loosened anywhere. Secretish keeps its three callers (two of them
// redactors in internal/redact, which must never be relaxed), Validate keeps refusing secret-shaped
// members in the config domain, and both enforcement points are untouched. The key simply never
// enters that domain — which is a stronger outcome than the relaxation the backlog record proposed.
//
// WRITE-FORWARD ONLY. A stored value is never returned by any route. A read reports whether a key is
// set, when it last changed, and a hint of at most the last four characters — enough for an
// administrator to tell "the key I meant" from "some other key", not enough to reconstruct one. That
// is not politeness: the reason a key may be typed into the UI at all is that it travels one way, so
// the browser never holds it after the request that set it.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// providerKeysFileName sits beside config.json under state/ — already a mounted volume in every
// compose file, so a key survives a container restart exactly as the SQLite databases beside it do.
const providerKeysFileName = "provider-keys.json"

// providerKeyEnv is the single source of truth for which keys exist and which environment variable
// each materialises into. Every consumer derives from this map rather than repeating a name: the
// accepted-name check, the refusal message, the run-environment layer, the UI schema and the gate all
// read it, so a key added here becomes settable, resolvable, rendered and tested without a second
// edit. The value side is the env var brain/llm.py already honours; nothing downstream learns a new
// name.
var providerKeyEnv = map[string]string{
	"llm_api_key":       "LLM_API_KEY",
	"anthropic_api_key": "ANTHROPIC_API_KEY",
	"openai_api_key":    "OPENAI_API_KEY",
}

// providerKeyNames returns the settable names, sorted — for the schema and for refusal messages, both
// read by people and neither allowed to reorder between calls.
func providerKeyNames() []string {
	out := make([]string, 0, len(providerKeyEnv))
	for k := range providerKeyEnv {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// storedProviderKey is one entry as persisted. The value lives on disk and on NO response; the json
// tag exists so the file round-trips, not so the struct can be handed to a client.
type storedProviderKey struct {
	Value     string `json:"value"`
	UpdatedAt string `json:"updated_at"`
}

// providerKeysDoc is the whole file. Versioned because a credential store that later needs re-shaping
// must be able to say which shape it is reading, rather than inferring it from the members present.
type providerKeysDoc struct {
	Version int                          `json:"version"`
	Keys    map[string]storedProviderKey `json:"keys"`
}

const providerKeysDocVersion = 1

func (s *server) providerKeysPath() string {
	return filepath.Join(s.repo, "state", providerKeysFileName)
}

// readProviderKeys loads the document. A missing file is the EMPTY document, not an error: "no key has
// been set" is the normal state of a fresh deployment and a run must never fail because of it. A
// CORRUPT file is also empty for the run path — but it is reported through the returned bool, so the
// API can tell an administrator their stored key is unreadable instead of answering "not set", which
// reads as "nothing was ever saved" and invites them to save over a file the server cannot parse.
func (s *server) readProviderKeys() (providerKeysDoc, bool) {
	empty := providerKeysDoc{Version: providerKeysDocVersion, Keys: map[string]storedProviderKey{}}
	b, err := os.ReadFile(s.providerKeysPath())
	if err != nil {
		return empty, true // absent -> empty, and absence is not corruption
	}
	var on providerKeysDoc
	if err := json.Unmarshal(b, &on); err != nil {
		return empty, false
	}
	if on.Keys == nil {
		on.Keys = map[string]storedProviderKey{}
	}
	if on.Version == 0 {
		on.Version = providerKeysDocVersion
	}
	return on, true
}

// writeProviderKeys persists atomically at 0600 — temp file in the same directory, then rename,
// exactly as writeConfigFile does. A half-written credential file is worse than none: the reader
// above would call it corrupt and every run would go out with no key at all.
//
// 0600 here is not defence-in-depth decoration, it IS the access control. config.json records that it
// is 0600 "because the deployment shape is not public either"; this file holds the credential itself.
func (s *server) writeProviderKeys(doc providerKeysDoc) error {
	path := s.providerKeysPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	doc.Version = providerKeysDocVersion
	enc, err := json.Marshal(doc)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".provider-keys-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op after a successful rename; cleans up every failure path
	// Chmod BEFORE the write. CreateTemp is already 0600, but stating it here means a change to that
	// assumption cannot silently widen the one file in the tree that holds a credential.
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(enc); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}

// providerKeyHint renders the at-most-four-character tail an administrator needs to tell WHICH key is
// stored. A short value gets no hint at all: revealing four characters of six is revealing the key.
// The threshold is on the raw length, so a short value cannot be probed by watching a hint appear.
func providerKeyHint(v string) string {
	if len(v) < 12 {
		return ""
	}
	return v[len(v)-4:]
}

// providerKeyStatus is what a read returns: presence, provenance, a hint. Never a value. The struct
// has no member that could hold one, so a later edit cannot leak a key by filling a field.
type providerKeyStatus struct {
	Set       bool   `json:"set"`
	Hint      string `json:"hint,omitempty"`
	UpdatedAt string `json:"updated_at,omitempty"`
	// FromEnv reports that the process environment supplies this key and therefore OVERRIDES whatever
	// is stored. Without it, an administrator who sets a key in the UI on a deployment whose compose
	// passes the same variable through would see "set" and be wrong about which key their runs use.
	FromEnv bool `json:"from_env"`
}

// providerKeysStatus builds the read model for every DECLARED key, so a key that exists is always
// reported — including one supplied only by the environment and never written here.
func (s *server) providerKeysStatus() (map[string]providerKeyStatus, bool) {
	doc, ok := s.readProviderKeys()
	out := make(map[string]providerKeyStatus, len(providerKeyEnv))
	for name, env := range providerKeyEnv {
		st := providerKeyStatus{FromEnv: os.Getenv(env) != ""}
		if e, has := doc.Keys[name]; has && e.Value != "" {
			st.Set, st.Hint, st.UpdatedAt = true, providerKeyHint(e.Value), e.UpdatedAt
		}
		out[name] = st
	}
	return out, ok
}

// providerKeyEnvLayer maps stored keys to their environment variables, for resolveRunEnv's LOWEST
// layer. Empty values are omitted so they cannot shadow a higher layer through the "present but
// empty" path resolveRunEnv documents.
func (s *server) providerKeyEnvLayer() map[string]string {
	doc, _ := s.readProviderKeys()
	out := map[string]string{}
	for name, e := range doc.Keys {
		env, known := providerKeyEnv[name]
		if !known || e.Value == "" {
			continue
		}
		out[env] = e.Value
	}
	return out
}

// handleGetProviderKeys answers WHICH keys are set, never what they are.
func (s *server) handleGetProviderKeys(w http.ResponseWriter, r *http.Request) {
	st, readable := s.providerKeysStatus()
	body := map[string]any{"keys": st, "names": providerKeyNames()}
	if !readable {
		body["readable"] = false
		body["error"] = fmt.Sprintf("%s exists but is not valid JSON — inspect or remove it", s.providerKeysPath())
	}
	writeJSON(w, http.StatusOK, body)
}

// putProviderKeyReq is ONE key per request: {"name": "...", "value": "..."}.
//
// The shape is not arbitrary. A flat name->value map would have been convenient for a settings panel
// saving three fields at once, but it cannot be projected onto a CLI verb: agentctl's apiVerb carries
// a `SecretField`, which reads exactly one named body member from STDIN rather than from a flag,
// because "a password on an argv is visible to every `ps` on the host and lands in shell history".
// A dynamic map has no fixed member for that mechanism to name. Choosing {name, value} means
// `agentctl provider-keys set --name llm_api_key --value-stdin` reuses machinery that already exists
// and is already tested, instead of this route growing a second way to send a credential.
//
// The secondary benefit is narrower blast radius: one credential per request body is one credential
// in flight, rather than three.
type putProviderKeyReq struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// handlePutProviderKeys sets or clears ONE key. An empty value CLEARS it, which expresses
// rotation-to-nothing without a second route shape.
//
// Only declared names are accepted. An unknown name is refused WITH the list rather than ignored:
// silently dropping `openai_key` (for `openai_api_key`) would leave an administrator certain they had
// stored a key that no run will ever read.
func (s *server) handlePutProviderKeys(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, maxConfigBytes+1))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "unreadable body"})
		return
	}
	if len(body) > maxConfigBytes {
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]any{"error": "provider-keys document too large"})
		return
	}
	// Decoded into a typed struct with exactly two string members, so a caller cannot smuggle a third
	// one past the name check by nesting it.
	var in putProviderKeyReq
	if err := json.Unmarshal(body, &in); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": fmt.Sprintf(
			"provider-keys: body must be {\"name\": one of %s, \"value\": \"…\" (\"\" clears it)}",
			strings.Join(providerKeyNames(), "|"))})
		return
	}
	name := strings.TrimSpace(in.Name)
	if _, known := providerKeyEnv[name]; !known {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": fmt.Sprintf(
			"provider-keys: unknown name %q — a name that is not stored is a key no run will ever read, so it is refused rather than dropped; settable names are %s",
			name, strings.Join(providerKeyNames(), ", "))})
		return
	}
	value := strings.TrimSpace(in.Value)
	doc, readable := s.readProviderKeys()
	if !readable {
		// Overwriting an unparseable file would destroy whatever it holds. Refuse and name the path:
		// the administrator can look at it, and nothing is lost by our declining.
		writeJSON(w, http.StatusConflict, map[string]any{"error": fmt.Sprintf(
			"provider-keys: %s exists but is not valid JSON; refusing to overwrite it — inspect or remove it first",
			s.providerKeysPath())})
		return
	}
	if value == "" {
		delete(doc.Keys, name)
	} else {
		doc.Keys[name] = storedProviderKey{
			Value:     value,
			UpdatedAt: time.Now().UTC().Format(time.RFC3339),
		}
	}
	if err := s.writeProviderKeys(doc); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{
			"error": fmt.Sprintf("provider-keys: %v", err)})
		return
	}
	// The response is a fresh READ, not an echo of the request. The one way a write handler could leak
	// a value is by reporting back what it was handed.
	st, _ := s.providerKeysStatus()
	writeJSON(w, http.StatusOK, map[string]any{"keys": st, "names": providerKeyNames(), "saved": true})
}
