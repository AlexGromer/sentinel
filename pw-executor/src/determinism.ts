/**
 * Sentinel pw-executor — screenshot determinism anchors (GAP-RISK-009).
 *
 * A golden `screenshot_hash` is the SHA-256 of the raw PNG bytes. For that hash to be byte-stable
 * across separate browser processes (baseline-capture run vs. later replay run) the render must be
 * pinned: a fixed viewport + DSR=1 (so layout/raster don't shift), animations frozen, the text caret
 * hidden, and CSS-pixel scaling. These constants are the single source of truth for those anchors;
 * `server.ts` consumes them at context-creation and screenshot time. They are exported (not inlined)
 * so a `node:test` can assert the determinism config can't silently regress — see determinism.test.ts.
 *
 * NOTE: byte-stability holds only in headless Chromium (ADR-037); headed / CDP-attach are observation
 * modes where the user's render path is reused and screenshots are NOT byte-stable.
 */

/** Fixed render box: layout and rasterization are viewport-dependent, so the golden pins it. */
export const DETERMINISM_VIEWPORT: { width: number; height: number } = { width: 1280, height: 720 };

/** Device scale factor = 1: HiDPI scaling changes pixel counts, which would change the PNG bytes. */
export const DETERMINISM_DEVICE_SCALE_FACTOR = 1;

/**
 * Options passed verbatim to `page.screenshot(...)`. `animations:'disabled'` freezes CSS animations
 * to their end state, `caret:'hide'` removes the blinking text caret, `scale:'css'` rasterizes at CSS
 * pixels (independent of the host DSR). Together they remove the known cross-process byte-flip sources.
 */
export const SCREENSHOT_DETERMINISM_OPTS: { animations: 'disabled'; caret: 'hide'; scale: 'css' } = {
  animations: 'disabled',
  caret: 'hide',
  scale: 'css',
};
