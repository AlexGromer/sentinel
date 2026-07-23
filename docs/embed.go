// Package webui embeds the browser UI so `control-api` can serve it from its own port
// (Mode 3, single-service — ADR-064). The files live here because that is where GitHub Pages
// publishes them from (.github/workflows/pages.yml builds ./docs) and go:embed patterns cannot
// reach outside the containing file's directory — hence a Go file inside docs/.
//
// SECURITY — the pattern list below is an explicit ALLOWLIST, never `all:.` or `*`:
// docs/ also holds INTERNAL-ONLY material (COMPETITIVE_ANALYSIS.internal.md,
// COMPETITIVE_ANALYSIS.raw.internal.json, DOC_BACKLOG.internal.md, …). Those are gitignored, so a
// wildcard embed would look clean in CI and silently bake them into a binary built on a maintainer's
// machine — the exact shape the "never publish liability docs" rule exists to prevent.
// TestEmbeddedUIHasNoInternalDocs (cmd/control-api/ui_test.go) fails if that allowlist is widened.
//
// The prose docs (*.md) are deliberately NOT embedded: the UI links them to GitHub, which renders
// markdown far better than a FileServer would.
package webui

import (
	"embed"
	"io/fs"
)

//go:embed index.html prices.json backend-presets.json
//go:embed setup chat calculators
var embedded embed.FS

// FS is the UI file tree, rooted so that "index.html", "setup/index.html", "prices.json" resolve
// exactly as they do on Pages and under the `webui` compose profile — the three pages fetch
// 'prices.json' and '../backend-presets.json' relatively and must not care who is serving them.
var FS fs.FS = embedded
