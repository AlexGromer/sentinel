package main

import "testing"

// SEC-CHATS-WIRING-GAP. cmdRun decided whether to start the store-gateway with a bare `if *replay`,
// so a chat run went to the storeless branch and _project_chat (brain/__main__.py) — which only
// writes the `chats` projection when make_chat_projector() sees STORE_ADDR — was silently a no-op
// for every chat run. The decision is now runNeedsStore(); this pins it so a regression that drops
// chat back out of the store path cannot pass.
//
// Kills: reverting the condition to `replay` alone (chat -> false again).
// Kills: widening it to explore, which would start a gateway no explore run needs.
func TestRunNeedsStore(t *testing.T) {
	cases := []struct {
		mode   string
		replay bool
		want   bool
		why    string
	}{
		{"chat", false, true, "chat writes the chats projection, which needs STORE_ADDR (the bug)"},
		{"replay", true, true, "replay reads the locator/golden/quarantine store"},
		{"explore", false, false, "explore spends no tokens and reads no store"},
		{"goal", false, false, "goal authoring runs without the gateway"},
		{"describe", false, false, "describe authoring runs without the gateway"},
		// replay is orthogonal to mode: a replay always needs the store regardless of the mode string.
		{"explore", true, true, "replay flag alone still needs the store"},
	}
	for _, c := range cases {
		if got := runNeedsStore(c.mode, c.replay); got != c.want {
			t.Errorf("runNeedsStore(%q, %v) = %v, want %v — %s", c.mode, c.replay, got, c.want, c.why)
		}
	}
}
