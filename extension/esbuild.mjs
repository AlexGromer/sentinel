// Build the MV3 extension into dist/ — one bundled IIFE per entry point, plus the static assets
// (manifest.json + the two HTML pages). No code-splitting: a Chrome extension loads each entry in its
// own world (service worker / content script / devtools page / panel), so each is bundled standalone.
//
// `node esbuild.mjs` for a one-shot build, `node esbuild.mjs --watch` for incremental rebuilds.
import * as esbuild from 'esbuild';
import { cp, mkdir, rm } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const dist = join(root, 'dist');
const watch = process.argv.includes('--watch');

// One entry per execution world. IIFE (not ESM) keeps the content script injectable via
// chrome.scripting.executeScript and avoids needing `"type":"module"` on the service worker.
const entries = {
  background: 'src/background/index.ts',
  content: 'src/content/recorder.ts',
  // ADR-138: the route journal is a FIFTH world — the page's own (`world: 'MAIN'`). It cannot be part
  // of `content` because the recorder runs ISOLATED, where the page's `history.pushState` is
  // invisible (measured), which is the entire reason this entry exists.
  'route-journal': 'src/content/route-journal-main.ts',
  devtools: 'src/devtools/devtools.ts',
  panel: 'src/devtools/panel.ts',
};

/** Copy the static assets (manifest + HTML pages) that aren't bundled. */
async function copyStatic() {
  await cp(join(root, 'manifest.json'), join(dist, 'manifest.json'));
  await cp(join(root, 'public'), dist, { recursive: true });
}

// ADR-143: the SHARED PROTOCOL, additionally emitted as ESM for the end-to-end check.
//
// ⚠ WHY A SECOND OUTPUT OF THE SAME SOURCE. `recorder.e2e.mjs` used to hand-write the subprotocol
// pair — `['sentinel.recorder.v1', 'bearer.' + token]` — so the one thing the #43 block exists to
// prove (that the client half of the bearer handshake is built correctly) was proven about a COPY
// living in the test, not about the code that ships. A copy agrees with itself forever. The e2e now
// imports `wsSubprotocols` from here, so changing the real one turns the check red.
//
// It cannot import the IIFE bundles above: those are built for `chrome.scripting.executeScript` and
// for a service worker that must not be `"type":"module"`, and neither exports anything.
const protocolOptions = {
  entryPoints: { protocol: join(root, 'src/shared/protocol.ts') },
  outdir: dist,
  outExtension: { '.js': '.mjs' },
  bundle: true,
  format: 'esm',
  target: 'node20',
  platform: 'neutral',
  sourcemap: true,
  logLevel: 'info',
};

const buildOptions = {
  entryPoints: Object.fromEntries(
    Object.entries(entries).map(([name, file]) => [name, join(root, file)]),
  ),
  outdir: dist,
  bundle: true,
  format: 'iife',
  target: 'chrome120',
  platform: 'browser',
  sourcemap: true,
  logLevel: 'info',
};

await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });

if (watch) {
  const ctx = await esbuild.context(buildOptions);
  const pctx = await esbuild.context(protocolOptions);
  await ctx.watch();
  await pctx.watch();
  await copyStatic();
  console.log('[esbuild] watching for changes…');
} else {
  await esbuild.build(buildOptions);
  await esbuild.build(protocolOptions);
  await copyStatic();
  console.log('[esbuild] build complete → dist/');
}
