package main

import "testing"

// TestDecideAuth pins the startup auth decision (#34/#35). The load-bearing case is the last one:
// no token and no --no-auth must be authRefuse (the gateway exits 2), never a silent no-auth serve.
func TestDecideAuth(t *testing.T) {
	cases := []struct {
		name   string
		token  string
		noAuth bool
		want   authMode
	}{
		{"token set -> authenticate", "secret", false, authToken},
		{"token wins over --no-auth", "secret", true, authToken},
		{"no token + --no-auth -> serve unauthenticated", "", true, authNoAuth},
		{"no token, no flag -> refuse (fail closed, exit 2)", "", false, authRefuse},
	}
	for _, c := range cases {
		if got := decideAuth(c.token, c.noAuth); got != c.want {
			t.Errorf("%s: decideAuth(%q, %v) = %v, want %v", c.name, c.token, c.noAuth, got, c.want)
		}
	}
}
