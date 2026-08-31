// MAIN-world entry point for the route journal (ADR-138). Injected by the service worker with
// `chrome.scripting.executeScript({ world: 'MAIN' })` alongside the ISOLATED recorder; this file
// exists only to run `installRouteJournal` against the page's own `window`, which is the one place
// where the page's `history.pushState` can be observed at all.
import { installRouteJournal, type JournalWindow } from './route-journal.js';

installRouteJournal(window as unknown as JournalWindow);
