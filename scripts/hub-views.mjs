/**
 * The hub's view names, READ FROM THE HUB (docs/DEVELOPMENT.md §0.5).
 *
 * WHY THIS MODULE EXISTS. Three places used to hold the same list: the hub itself, the DOM gate and
 * the UI smoke. The smoke's copy had seven of nine — `tools` and `settings` were never screenshotted,
 * ever — and the omission survived a deliberate edit: when `journal` was added the list was extended
 * and nobody noticed the two that were missing.
 *
 * That is not carelessness, it is how a hand-kept list FAILS. An extra entry is visible: a row for a
 * view that no longer exists breaks the run. A MISSING entry is not, because absence has no
 * representation to look at — a list of seven names looks complete, because it looks like a list.
 *
 * So the list is derived from the one place that cannot be wrong about it: `var VIEWS = [...]` in
 * docs/index.html, which is what `setView()` validates against. A view the router will not open is
 * not a view, and a view the router WILL open is now covered by construction.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

/**
 * Minimum number of views. A derived list removes the "somebody forgot to add it" failure and
 * introduces a quieter one: a regex that stops matching yields an EMPTY list, and every check over it
 * passes perfectly. The floor is the only thing that catches that, so it is not optional.
 */
export const MIN_VIEWS = 9;

/** Every view name the hub's router will accept, in the hub's own order. */
export function hubViews() {
  const hub = fs.readFileSync(path.join(REPO, 'docs', 'index.html'), 'utf8');
  const m = /var VIEWS\s*=\s*\[([^\]]*)\]/.exec(hub);
  if (!m) {
    throw new Error(
      'docs/index.html has no `var VIEWS = [...]` — the view list could not be derived. This is a ' +
      'hard failure on purpose: falling back to a built-in list would restore exactly the hand-kept ' +
      'list this module exists to remove.',
    );
  }
  const views = m[1]
    .split(',')
    .map((v) => v.trim().replace(/^['"]|['"]$/g, ''))
    .filter(Boolean);
  if (views.length < MIN_VIEWS) {
    throw new Error(
      `derived only ${views.length} view(s) from docs/index.html, expected at least ${MIN_VIEWS} — ` +
      'the parser, not the hub, is what regressed. A check that walks an empty list passes silently.',
    );
  }
  return views;
}
