package main

import (
	"os"
	"strings"
	"testing"
)

// envMap turns filteredEnv()'s []"K=V" into a lookup keyed by name.
func envMap(kvs []string) map[string]string {
	m := make(map[string]string, len(kvs))
	for _, kv := range kvs {
		if i := strings.IndexByte(kv, '='); i >= 0 {
			m[kv[:i]] = kv[i+1:]
		}
	}
	return m
}

// TestFilteredEnvDefaultOn locks the M11.3 (ADR-035) flip: with SENTINEL_ENV_ALLOWLIST *absent*
// the allowlist must already filter — unrelated host secrets are dropped, the curated families +
// SENTINEL_ENV_ALLOW extras pass. (The previous default was full passthrough; this guards the regress.)
func TestFilteredEnvDefaultOn(t *testing.T) {
	// Force the var absent (cannot be done via t.Setenv) so we exercise the genuine default path.
	if orig, ok := os.LookupEnv("SENTINEL_ENV_ALLOWLIST"); ok {
		if err := os.Unsetenv("SENTINEL_ENV_ALLOWLIST"); err != nil {
			t.Fatalf("unset SENTINEL_ENV_ALLOWLIST: %v", err)
		}
		t.Cleanup(func() { _ = os.Setenv("SENTINEL_ENV_ALLOWLIST", orig) })
	}

	t.Setenv("AWS_SECRET_ACCESS_KEY", "AKIAleak") // unrelated host secret — MUST be dropped
	t.Setenv("ANTHROPIC_API_KEY", "present")      // exact allowlist (value is a non-secret placeholder)
	t.Setenv("LLM_FOO", "1")                      // LLM_ prefix
	t.Setenv("OTEL_X", "1")                       // OTEL_ prefix
	t.Setenv("PW_Y", "1")                         // PW_ prefix
	t.Setenv("PROM_PUSHGATEWAY", "pg:9091")       // M11.3 curated exact
	t.Setenv("HTTPS_PROXY", "http://proxy:3128")  // M11.3 curated exact
	t.Setenv("SSL_CERT_FILE", "/etc/ssl/ca.pem")  // M11.3 curated exact
	t.Setenv("SENTINEL_ENV_ALLOW", "AUT_PASSWORD,CUSTOM_X")
	t.Setenv("AUT_PASSWORD", "hunter2") // allowed only via SENTINEL_ENV_ALLOW
	t.Setenv("CUSTOM_X", "y")           // allowed only via SENTINEL_ENV_ALLOW

	got := envMap(filteredEnv())

	for _, name := range []string{
		"PATH", "ANTHROPIC_API_KEY", "LLM_FOO", "OTEL_X", "PW_Y",
		"PROM_PUSHGATEWAY", "HTTPS_PROXY", "SSL_CERT_FILE", "AUT_PASSWORD", "CUSTOM_X",
	} {
		if _, ok := got[name]; !ok {
			t.Errorf("default-on: %q should pass the allowlist but was dropped", name)
		}
	}
	if _, ok := got["AWS_SECRET_ACCESS_KEY"]; ok {
		t.Error("default-on: AWS_SECRET_ACCESS_KEY leaked — unrelated host secret must be filtered")
	}
}

// TestFilteredEnvOptOut verifies the escape hatch: SENTINEL_ENV_ALLOWLIST=0 → full passthrough,
// so even an unrelated secret survives (for debugging / unusual local setups).
func TestFilteredEnvOptOut(t *testing.T) {
	t.Setenv("SENTINEL_ENV_ALLOWLIST", "0")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "AKIApass")

	got := envMap(filteredEnv())
	if _, ok := got["AWS_SECRET_ACCESS_KEY"]; !ok {
		t.Error("opt-out (=0): AWS_SECRET_ACCESS_KEY must pass through unfiltered")
	}
}
