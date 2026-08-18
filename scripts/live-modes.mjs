/**
 * The live area's mode names, READ FROM THE HUB (docs/DEVELOPMENT.md §0.5).
 *
 * WHY THIS MODULE EXISTS. FOUR places held the same list `['frame','actions','video']`: the hub twice
 * (docs/index.html — once to toggle the panes, once to wire the clicks), the DOM gate twice
 * (scripts/hub-dom-check.mjs) and the UI smoke once (scripts/ui-smoke.mjs). That is one more copy than
 * the view list had when it was measured to be wrong.
 *
 * A MODE LIST FAILS DIFFERENTLY FROM THE VIEW LIST, AND WORSE. A view that is in `VIEWS` and missing
 * from the markup fails the first time anyone opens it, because the router validates against `VIEWS`.
 * A live mode has no router: the JS list decides which panes toggle and which buttons are wired, the
 * markup decides which buttons EXIST, and nothing compares the two. A button present in the markup and
 * absent from the list sits on screen, takes clicks, and does nothing at all — and no check that walks
 * the JS list can see it, because it never clicks it.
 *
 * So this module does both halves: it derives the list from the single declaration
 * `var LIVE_MODES = [...]`, and it REFUSES when that list and the `data-lvmode` buttons disagree.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

/**
 * Minimum number of live modes. Same reason as MIN_VIEWS in hub-views.mjs: a regex that stops matching
 * yields an EMPTY list, and every check over it passes perfectly. Set at the measured number, and it
 * only ever goes UP.
 */
export const MIN_LIVE_MODES = 4;   // LIVE-VNC added `screen` (frame · actions · video · screen)

/** Every live-area mode the hub declares, in the hub's own order. [0] is the default mode. */
export function liveModes() {
  const hub = fs.readFileSync(path.join(REPO, 'docs', 'index.html'), 'utf8');
  const m = /var LIVE_MODES\s*=\s*\[([^\]]*)\]/.exec(hub);
  if (!m) {
    throw new Error(
      'docs/index.html has no `var LIVE_MODES = [...]` — the live-mode list could not be derived. ' +
      'This is a hard failure on purpose: falling back to a built-in list would restore exactly the ' +
      'four hand-kept copies this module exists to remove.',
    );
  }
  const modes = m[1]
    .split(',')
    .map((v) => v.trim().replace(/^['"]|['"]$/g, ''))
    .filter(Boolean);
  if (modes.length < MIN_LIVE_MODES) {
    throw new Error(
      `derived only ${modes.length} live mode(s) from docs/index.html, expected at least ` +
      `${MIN_LIVE_MODES} — the parser, not the hub, is what regressed. A check that walks an empty ` +
      'list passes silently.',
    );
  }
  // THE SECOND HALF — see the header. Compared as SETS, not sequences: reordering the tabs is a UX
  // decision, disagreeing about which tabs exist is a defect.
  const inMarkup = [...hub.matchAll(/data-lvmode="([\w-]+)"/g)].map((x) => x[1]);
  const only = (a, b) => a.filter((x) => !b.includes(x));
  const orphanModes = only(modes, inMarkup);
  const orphanButtons = only(inMarkup, modes);
  if (orphanModes.length || orphanButtons.length) {
    throw new Error(
      "the hub's LIVE_MODES and its data-lvmode buttons disagree — " +
      `declared with no button: ${JSON.stringify(orphanModes)}; ` +
      `a button with no declaration: ${JSON.stringify(orphanButtons)}. ` +
      'The second case is why this comparison exists: such a button is on screen, is clickable, is ' +
      'wired to nothing, and looks exactly like one that works.',
    );
  }
  return modes;
}
