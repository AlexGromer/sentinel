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
  devtools: 'src/devtools/devtools.ts',
  panel: 'src/devtools/panel.ts',
};

/** Copy the static assets (manifest + HTML pages) that aren't bundled. */
async function copyStatic() {
  await cp(join(root, 'manifest.json'), join(dist, 'manifest.json'));
  await cp(join(root, 'public'), dist, { recursive: true });
}

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
  await ctx.watch();
  await copyStatic();
  console.log('[esbuild] watching for changes…');
} else {
  await esbuild.build(buildOptions);
  await copyStatic();
  console.log('[esbuild] build complete → dist/');
}
