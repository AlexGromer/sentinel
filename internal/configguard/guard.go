// Package configguard is the single source of truth for "what must never be persisted in the config
// domain" (M11.5 PR-5, ADR-062).
//
// Both enforcement points import it: the store-gateway (internal/store, which owns the boundary — the
// gRPC socket is reachable by any same-UID process holding STORE_TOKEN) and the control-API (which
// applies it first so a caller gets a 400 with the offending path instead of an opaque gRPC error).
// One rule, one place: a second copy would drift, which is exactly the failure ADR-059 warns about.
//
// The rule is name-based and refuses rather than strips. Silently removing a key would leave the
// operator believing a secret was stored and honoured; refusing tells them where it goes instead
// (the process env).
package configguard

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

// MaxConfigBytes bounds a config document. It is a form, not a payload. Enforced at BOTH the gateway
// (the trust boundary, reachable by any STORE_TOKEN holder) and the control-API's HTTP layer.
const MaxConfigBytes = 64 << 10

// secretNameParts are matched as substrings of a lower-cased JSON member name.
var secretNameParts = []string{
	"password", "passwd", "secret", "credential", "api_key", "apikey", "private_key", "bearer",
}

// Secretish reports whether a JSON member name looks like a credential.
//
// The name is first canonicalized: lower-cased, and every run of non-alphanumeric separators (`-`, ` `,
// `.`, etc.) collapsed to a single `_`. Without this, `api-key`, `api key` and `api.key` would each slip
// past the `api_key` substring test (they contain no underscore) — a real credential-smuggling hole.
//
// The `token` and `key` families are then matched on `_`-delimited word boundaries rather than as
// substrings: `max_tokens` and `total_tokens` are ordinary counters, and a substring rule would refuse a
// legitimate document.
func Secretish(name string) bool {
	n := canonName(name)
	for _, part := range secretNameParts {
		if strings.Contains(n, part) {
			return true
		}
	}
	if hasWord(n, "token") || hasWord(n, "key") {
		return true
	}
	return false
}

// canonName lower-cases and collapses every maximal run of non-[a-z0-9] characters to one `_`, trimming
// leading/trailing `_`. "API-Key" -> "api_key", "api . key" -> "api_key", "apiKey" -> "apikey".
func canonName(name string) string {
	var b strings.Builder
	prevSep := false
	for _, r := range strings.ToLower(name) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
			prevSep = false
		} else if !prevSep {
			b.WriteByte('_')
			prevSep = true
		}
	}
	return strings.Trim(b.String(), "_")
}

// hasWord reports whether `word` appears as a whole `_`-delimited token in an already-canonical name.
func hasWord(canon, word string) bool {
	for _, seg := range strings.Split(canon, "_") {
		if seg == word {
			return true
		}
	}
	return false
}

// FindSecretKey walks a decoded JSON document and returns the dotted path of the first secret-shaped
// member name, or "" when the document is clean. Nested objects and arrays are covered. Member names
// are visited in sorted order so the reported path is deterministic for a given document.
func FindSecretKey(v any, path string) string {
	switch t := v.(type) {
	case map[string]any:
		names := make([]string, 0, len(t))
		for k := range t {
			names = append(names, k)
		}
		sort.Strings(names)
		for _, k := range names {
			p := k
			if path != "" {
				p = path + "." + k
			}
			if Secretish(k) {
				return p
			}
			if hit := FindSecretKey(t[k], p); hit != "" {
				return hit
			}
		}
	case []any:
		for i, e := range t {
			if hit := FindSecretKey(e, fmt.Sprintf("%s[%d]", path, i)); hit != "" {
				return hit
			}
		}
	}
	return ""
}

// Validate reports why valueJSON may not be persisted, or nil when it may.
//
// A non-object document is refused outright: a bare JSON string ("sk-live-…") carries no member names
// for a name-based guard to inspect, so accepting one would be a hole straight through the guard.
func Validate(valueJSON string) error {
	if len(valueJSON) > MaxConfigBytes {
		return fmt.Errorf("config: value_json is %d bytes, over the %d-byte limit", len(valueJSON), MaxConfigBytes)
	}
	var doc any
	if err := json.Unmarshal([]byte(valueJSON), &doc); err != nil {
		return fmt.Errorf("config: value_json is not valid JSON: %w", err)
	}
	if _, isObj := doc.(map[string]any); !isObj {
		return fmt.Errorf("config: value_json must be a JSON object")
	}
	if hit := FindSecretKey(doc, ""); hit != "" {
		return fmt.Errorf("config: refusing to persist secret-shaped key %q — secrets live in the process env, never in the store", hit)
	}
	return nil
}
