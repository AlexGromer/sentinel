package main

// ADR-111 — the live VIDEO mode, proxied from the browser service.
//
// The live area has had three modes since ADR-108d and only two of them existed: the frame-per-step
// view and the action list. The video mode rendered a paragraph explaining it was not built, which
// was honest and useless. What blocked it was not the browser — the executor has carried screencast
// tools since then — but the ABSENCE OF A CHANNEL: the executor runs inside the brain's process on
// stdio, so control-api has no address for it and never will.
//
// ADR-110 made the browser a service, which inverts the problem. The process holding the browser is
// now long-lived and already listening, so it serves its own screencast (pw-executor/src/cdp-service.ts)
// and control-api does the one thing it is placed to do: put a credential in front of it.
//
// WHY A PROXY AND NOT A CDP CLIENT HERE. control-api would have to speak CDP over a WebSocket, as a
// CLIENT. Its ws.go is a hand-rolled websocket SERVER — there is no websocket library in go.mod, and
// adding one has broken the air-gapped build before (the same obstacle that sent ADR-109's password
// hashing to the stdlib). Writing a second, client-side implementation of the framing protocol to
// reach a browser that a Node process already holds a session to would be a lot of protocol code
// bought for nothing.
//
// ⚠ WHAT THIS PROXY IS FOR, precisely: the browser service's live port is unauthenticated, exactly
// like its CDP port, and for the same reason — it is on an internal network and nothing else. This
// route is how a person reaches it, and it carries the same bearer credential as every other route.
// Neither port is ever published to the host.

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// liveBase is the browser service's live endpoint, e.g. http://browser:9224. Empty means no browser
// service is configured, which is the normal single-container deployment — the video mode then says
// so rather than failing in a way that reads as broken.
// liveStatusPath is the browser service's own status endpoint, named ONCE. Both callers derive it
// from here: handleLiveStatus (which proxies it to the hub) and probeBrowser (readyz.go).
//
// ⚠ Bought by a live run. The readiness probe first guessed "/status" and got a 404 from the real
// service; every unit test passed, because the fake server in them answered any path at all. Two
// statements of one path is the same class as two statements of one message format — and the same
// cure: one declaration, no copy to drift from.
const liveStatusPath = "/live/status"

func liveBase() string {
	return strings.TrimRight(os.Getenv("CONTROL_API_CDP_LIVE"), "/")
}

// liveClient has NO overall timeout on purpose: /live/mjpeg is an open-ended stream, and a client
// timeout would cut the picture at a fixed age for no reason the viewer could understand. The
// connect phase is bounded instead, which is the part that can hang on a dead service.
var liveClient = &http.Client{
	Transport: &http.Transport{
		DialContext:           (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
		ResponseHeaderTimeout: 10 * time.Second,
	},
}

// liveTargetURL builds the browser-service URL for one request, and is the ONLY place that does.
//
// LIVE-PER-RUN found out why that matters: the query string was added to proxyLive and NOT to
// handleLiveStatus, because the two built their target independently. Nothing failed — status simply
// went on answering about the newest page while the frame path answered about the run. Two places
// deciding one thing is how they come to disagree, and the disagreement is invisible.
//
// Only run_id is forwarded, never the whole query: this proxy must not become a way to hand
// arbitrary parameters to the browser service's surface.
func liveTargetURL(base, path string, r *http.Request) string {
	target := base + path
	if runID := strings.TrimSpace(r.URL.Query().Get("run_id")); runID != "" {
		target += "?run_id=" + url.QueryEscape(runID)
	}
	return target
}

func (s *server) handleLiveStatus(w http.ResponseWriter, r *http.Request) {
	base := liveBase()
	if base == "" {
		// 200, not an error: "this deployment has no browser service" is a true answer to "what is
		// the state of the live view", and the UI needs to tell the two apart from "it broke".
		writeJSON(w, http.StatusOK, map[string]any{
			"available": false,
			"reason":    "no browser service configured (CONTROL_API_CDP_LIVE is unset)",
		})
		return
	}
	// The request-construction error is CHECKED, not dropped: `base` comes from an operator's
	// CONTROL_API_CDP_LIVE, so a typo makes NewRequest return nil, and `Do(nil)` dereferences it. A
	// config mistake would reach the operator as a stack trace instead of as the sentence below —
	// and proxyLive two functions down already gets this right, which made it an inconsistency
	// rather than a considered choice.
	req, rerr := http.NewRequestWithContext(r.Context(), http.MethodGet, liveTargetURL(base, liveStatusPath, r), nil)
	if rerr != nil {
		writeJSON(w, http.StatusOK, map[string]any{
			"available": false,
			"reason":    fmt.Sprintf("CONTROL_API_CDP_LIVE is not a usable URL (%q): %v", base, rerr),
		})
		return
	}
	resp, err := liveClient.Do(req)
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]any{
			"available": false,
			"reason":    fmt.Sprintf("the browser service at %s did not answer: %v", base, err),
		})
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<10))
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	// The upstream document, wrapped with `available` so the caller never has to infer availability
	// from the shape of what came back.
	fmt.Fprintf(w, `{"available":true,"upstream":%s}`, strings.TrimSpace(string(body)))
}

func (s *server) handleLiveFrame(w http.ResponseWriter, r *http.Request) {
	s.proxyLive(w, r, "/live/frame.jpg", false)
}

func (s *server) handleLiveStream(w http.ResponseWriter, r *http.Request) {
	s.proxyLive(w, r, "/live/mjpeg", true)
}

// proxyLive forwards one request to the browser service. `stream` selects flush-per-write, which is
// what makes multipart/x-mixed-replace arrive as a stream instead of as one buffered blob at the end.
func (s *server) proxyLive(w http.ResponseWriter, r *http.Request, path string, stream bool) {
	base := liveBase()
	if base == "" {
		// 501, not 404: the route exists and the deployment does not implement it. A 404 would read
		// as "wrong URL" and send the reader looking for a typo.
		http.Error(w, "no browser service configured (CONTROL_API_CDP_LIVE is unset) — "+
			"start the `browser` compose profile and point control-api at it", http.StatusNotImplemented)
		return
	}
	ctx := r.Context()
	if !stream {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, 20*time.Second)
		defer cancel()
	}
	// LIVE-PER-RUN. The QUERY STRING IS CARRIED, and it did not used to be: `path` was a literal here,
	// so `?run_id=` added by the hub or the CLI reached this function and was dropped on the floor.
	// Nothing failed — the browser service simply answered about the newest page, which is the exact
	// shape of defect this task exists to remove: a request that looks answered and is about
	// something else.
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, liveTargetURL(base, path, r), nil)
	if err != nil {
		http.Error(w, "bad live endpoint: "+err.Error(), http.StatusInternalServerError)
		return
	}
	resp, err := liveClient.Do(req)
	if err != nil {
		http.Error(w, "the browser service did not answer: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	if ct := resp.Header.Get("Content-Type"); ct != "" {
		w.Header().Set("Content-Type", ct)
	}
	// The service says WHOSE picture this is; a proxy that ate those headers would leave the caller
	// unable to check it got the run it asked for, which is the same silence in a different place.
	for _, h := range []string{"X-Sentinel-Run", "X-Sentinel-Scoped"} {
		if v := resp.Header.Get(h); v != "" {
			w.Header().Set(h, v)
		}
	}
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(resp.StatusCode)

	if !stream {
		_, _ = io.Copy(w, io.LimitReader(resp.Body, 8<<20))
		return
	}
	flusher, _ := w.(http.Flusher)
	buf := make([]byte, 32<<10)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return // the viewer closed the tab; upstream is released by the deferred Close
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if rerr != nil {
			return
		}
	}
}
