/**
 * What a run OWNS inside a browser it did not launch (ADR-128).
 *
 * WHY THIS FILE EXISTS AT ALL. Until ADR-128 the executor adopted `pages()[0]` in CDP-attach mode,
 * so "the run's page" meant "whatever tab was open first". Two concurrent runs therefore drove ONE
 * tab and announced ONE Chromium `targetId` — measured live, both `84DC6185` — and the live view
 * (ADR-121) had to refuse BOTH of them, because a picture true for one run and false for the other
 * is worse than no picture. A run now opens its own page instead, which raises two questions that
 * did not exist while everything in sight belonged to somebody else:
 *
 *   1. When a page appears in a context we adopted, is it OURS or the human's?
 *   2. Who closes the page we opened?
 *
 * Both answers are PURE PREDICATES here rather than conditions inline in `ensureBrowser`, and that
 * is deliberate: `server.ts` has no unit tests at all, because everything in it needs a live
 * Chromium. `launch.ts::resolveLaunchPlan` is the house pattern — the decision is separated from the
 * I/O precisely so a mutation to it goes red offline. A boolean buried in a call to `context.on`
 * can only be checked by a test that starts a browser, and the measured history of this repository
 * is that such checks get written for the case somebody thought of and for no other.
 */

/** Whether a page that just appeared in this run's context belongs to the run. */
export function shouldTrackNewPage(o: { attachedOverCDP: boolean; openerIsOurs: boolean }): boolean {
  // Launch mode: WE created the context and nothing else can put a page in it, so every page in it
  // is ours by construction. Asking about the opener there would be asking a question whose answer
  // is fixed — and would put an `await` on a path that is synchronous today.
  if (!o.attachedOverCDP) return true;
  // CDP-attach: the context is the human's. A popup our page opened is part of the run; a tab the
  // human opened by hand while the run was going is not, and treating it as ours would mean
  // `browser.switchTab` could drive their banking tab and `attachAppCapture` (ADR-067) would copy
  // their console into `runs/<id>/`. Both are the same overreach the own-page change removes — this
  // is that decision applied to the pages that appear LATER, not just to the first one.
  return o.openerIsOurs;
}

/**
 * Whether teardown should hand this run's pages back.
 *
 * ⚠ PAGES, PLURAL, AND THAT WAS BOUGHT BY A MEASUREMENT. The first version closed only the page the
 * run opened first, and a run driving `testdata/fixtures/l6-newtab.html` left its popup behind:
 * after shutdown the browser still held `l1.html`. Half a leak is still a leak — a run that opens
 * three tabs abandons three. What the run owns is what `pages[]` holds, and by construction that is
 * exactly its own page plus the popups its own pages raised (see shouldTrackNewPage).
 *
 * ⚠ THE BROWSER AND THE CONTEXT ARE NEVER CLOSED HERE — that guard (ADR-037) is unchanged and lives
 * in `server.ts`. What changed is that pages are now OURS, and leaving them open is not politeness
 * but a leak: the browser service (ADR-110) outlives every run, so abandoned tabs grow without
 * bound, and `currentPage()` — the answer to an unnamed live request — would keep picking a dead
 * run's page as "the newest".
 *
 * `contextClosed` is checked even though it cannot be true together with `attachedOverCDP` today:
 * the only thing that closes the context is `browser.videoStop`, and recording is refused over CDP
 * before the run starts (ADR-125). The two facts are independent, so the guard is written rather
 * than inferred — closing a page of a closed context throws, and the day that combination becomes
 * reachable it must not turn teardown into an error path.
 */
export function shouldClosePagesOnTeardown(
  o: { attachedOverCDP: boolean; havePages: boolean; contextClosed: boolean },
): boolean {
  return o.attachedOverCDP && o.havePages && !o.contextClosed;
}
