// Pure DOM → selector-candidate + redaction logic for the recorder (#44, M9.8 §2). NO chrome.* here —
// only the DOM — so it is unit-testable under jsdom and bundles into the content script untouched.
//
// Two jobs, both security- and grounding-critical:
//   1. buildSelectorCandidates — a ranked list of REAL locators for an element (testid → role+name →
//      label → text → css → xpath), the same L1–L6 spirit as the planner. record_bridge.py ranks these
//      by prior and grounds against them; we never fabricate, and we always emit a css/xpath fallback so
//      every captured element has at least one usable candidate.
//   2. classifyField — mandatory redaction. A password / secret field's value is NEVER returned; the
//      event carries a secretRef (env-var name) instead, exactly like pw-executor's secretRef (M9.1).

import type { Locator, RecorderEvent, RecorderEventType, SelectorCandidate, Strategy } from '../shared/protocol.js';

// ---------------------------------------------------------------------------------------------------
// Redaction (mandatory — issue #44 acceptance: a password value never appears in the event)
// ---------------------------------------------------------------------------------------------------

// Secret-field name tokens, matched per camelCase/separator token AND against the joined form, anchored
// (^…$) so "password"/"userPassword"/"api_key"/"ssn"/"cvv" redact while "passenger"/"passport"/
// "className"/"compass" do NOT (an unanchored substring regex over-redacts and breaks replay).
const SECRET_TOKEN_RE =
  /^(passwords?|passwd|pwd|passcode|passphrase|secret|otp|cvv|cvc|cardnumber|ccnumber|ssn|securitycode|pin|token|apikey|accesstoken|privatekey)$/i;
// autocomplete values that mark a genuinely sensitive field (WHATWG autofill tokens).
const SECRET_AUTOCOMPLETE = new Set(['current-password', 'new-password', 'one-time-code', 'cc-number', 'cc-csc']);

/** True if a field name/id reads as a secret — token-wise, not substring-wise. */
function nameLooksSecret(s: string): boolean {
  if (!s) return false;
  const tokens = s.replace(/([a-z0-9])([A-Z])/g, '$1 $2').split(/[^A-Za-z0-9]+/).filter(Boolean);
  const joined = s.replace(/[^A-Za-z0-9]+/g, '');
  return joined !== '' && (SECRET_TOKEN_RE.test(joined) || tokens.some((t) => SECRET_TOKEN_RE.test(t)));
}

export interface FieldClass {
  secret: boolean;
  /** when secret: the env-var name the bridge fills from (data-sentinel-secret value, or derived). */
  secretRef?: string;
}

/** Decide whether an element is a secret field whose value must be redacted, and the ref to record. */
export function classifyField(el: Element): FieldClass {
  const input = el as HTMLInputElement;
  const type = (input.type || el.getAttribute('type') || '').toLowerCase();
  const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
  const explicit = el.getAttribute('data-sentinel-secret');
  const name = input.name || el.getAttribute('name') || '';
  const id = el.id || '';

  const isSecret =
    type === 'password' ||
    SECRET_AUTOCOMPLETE.has(autocomplete) ||
    explicit !== null ||
    nameLooksSecret(name) ||
    nameLooksSecret(id);

  if (!isSecret) return { secret: false };
  return { secret: true, secretRef: deriveSecretRef(explicit, name || id) };
}

/** How an element should be driven on replay — so the recorder emits the right verb (a <select> needs
 * selectOption, a checkbox/radio is a click, not a fill — Playwright fill() rejects both). */
export type ControlKind = 'select' | 'toggle' | 'fillable' | 'other';

export function controlKind(el: Element): ControlKind {
  const tag = el.tagName.toLowerCase();
  if (tag === 'select') return 'select';
  if (tag === 'textarea') return 'fillable';
  if ((el as HTMLElement).isContentEditable) return 'fillable';
  if (tag === 'input') {
    const type = ((el as HTMLInputElement).type || el.getAttribute('type') || 'text').toLowerCase();
    if (type === 'checkbox' || type === 'radio') return 'toggle';
    if (['button', 'submit', 'reset', 'image', 'file', 'range', 'color'].includes(type)) return 'other';
    return 'fillable'; // text/email/tel/url/search/password/number/date/…
  }
  return 'other';
}

/** An env-var-ish name for a redacted field: the explicit marker wins; else SCREAMING_SNAKE of name/id. */
function deriveSecretRef(explicit: string | null, nameOrId: string): string {
  const raw = (explicit && explicit.trim()) || nameOrId;
  const env = raw
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .replace(/[^A-Za-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .toUpperCase();
  return env || 'SECRET';
}

// ---------------------------------------------------------------------------------------------------
// Selector candidates (ranked, real, never fabricated)
// ---------------------------------------------------------------------------------------------------

const TESTID_ATTRS = ['data-testid', 'data-test-id', 'data-test', 'data-qa', 'data-cy'];

/** Implicit ARIA role for the common interactive elements (enough to ground clicks/fills). */
function implicitRole(el: Element): string {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  switch (tag) {
    case 'a':
      return el.hasAttribute('href') ? 'link' : '';
    case 'button':
      return 'button';
    case 'select':
      return 'combobox';
    case 'textarea':
      return 'textbox';
    case 'input':
      if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (['', 'text', 'email', 'tel', 'url', 'search', 'password', 'number'].includes(type)) return 'textbox';
      return '';
    default:
      return '';
  }
}

/** Accessible name, pragmatically: aria-label → aria-labelledby → <label> → placeholder → text → title.
 * `allowText=false` skips the textContent fallback — used for editable/secret elements, whose textContent
 * IS the user's typed content (a contenteditable secret would otherwise leak into the name). */
export function accessibleName(el: Element, allowText = true): string {
  const aria = el.getAttribute('aria-label');
  if (aria && aria.trim()) return aria.trim();

  const labelledby = el.getAttribute('aria-labelledby');
  if (labelledby) {
    const txt = labelledby
      .split(/\s+/)
      .map((id) => el.ownerDocument?.getElementById(id)?.textContent?.trim() || '')
      .filter(Boolean)
      .join(' ');
    if (txt) return txt;
  }

  const labelText = associatedLabelText(el);
  if (labelText) return labelText;

  const placeholder = el.getAttribute('placeholder');
  if (placeholder && placeholder.trim()) return placeholder.trim();

  if (allowText) {
    const text = (el.textContent || '').trim().replace(/\s+/g, ' ');
    if (text && text.length <= 80) return text;
  }

  const title = el.getAttribute('title');
  if (title && title.trim()) return title.trim();
  return '';
}

/** Text of a <label for=id> or a wrapping <label>, if any. */
function associatedLabelText(el: Element): string {
  const doc = el.ownerDocument;
  if (el.id && doc) {
    const forLabel = doc.querySelector(`label[for="${cssEscape(el.id)}"]`);
    if (forLabel?.textContent) return forLabel.textContent.trim().replace(/\s+/g, ' ');
  }
  const wrapping = el.closest('label');
  if (wrapping?.textContent) return wrapping.textContent.trim().replace(/\s+/g, ' ');
  return '';
}

/** A short CSS path: #id when present; else a bounded tag:nth-of-type chain up to an id'd ancestor/body. */
export function cssPath(el: Element): string {
  if (el.id) return `#${cssEscape(el.id)}`;
  const parts: string[] = [];
  let node: Element | null = el;
  let depth = 0;
  while (node && node.nodeType === 1 && depth < 6) {
    if (node.id) {
      parts.unshift(`#${cssEscape(node.id)}`);
      return parts.join(' > ');
    }
    const tag = node.tagName.toLowerCase();
    const parent: Element | null = node.parentElement;
    if (!parent) {
      parts.unshift(tag);
      break;
    }
    const sameTag = Array.from(parent.children).filter((c) => c.tagName === node!.tagName);
    const part = sameTag.length > 1 ? `${tag}:nth-of-type(${sameTag.indexOf(node) + 1})` : tag;
    parts.unshift(part);
    if (parent.tagName.toLowerCase() === 'body') {
      parts.unshift('body');
      break;
    }
    node = parent;
    depth++;
  }
  return parts.join(' > ');
}

/** Absolute XPath (last-resort, always available). */
export function xpathOf(el: Element): string {
  const parts: string[] = [];
  let node: Element | null = el;
  while (node && node.nodeType === 1) {
    const tag = node.tagName.toLowerCase();
    const parent: Element | null = node.parentElement;
    if (!parent) {
      parts.unshift(`/${tag}`);
      break;
    }
    const sameTag = Array.from(parent.children).filter((c) => c.tagName === node!.tagName);
    const idx = sameTag.indexOf(node) + 1;
    parts.unshift(`/${tag}[${idx}]`);
    node = parent;
  }
  return parts.join('');
}

/** CSS.escape with a regex fallback (jsdom/older engines may lack the global). */
function cssEscape(s: string): string {
  const g = globalThis as unknown as { CSS?: { escape?: (v: string) => string } };
  if (g.CSS?.escape) return g.CSS.escape(s);
  return s.replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`);
}

/** Ranked, de-duplicated selector candidates for an element. Always ≥1 (css + xpath fallbacks). */
export function buildSelectorCandidates(el: Element): SelectorCandidate[] {
  const out: SelectorCandidate[] = [];
  const push = (strategy: Strategy, locator: Locator) => out.push({ strategy, locator });

  // For an editable/secret element, its textContent is the user's typed input — never derive a name or
  // text locator from it (a contenteditable secret would otherwise leak into selectorCandidates).
  const editable = (el as HTMLElement).isContentEditable || classifyField(el).secret;

  for (const attr of TESTID_ATTRS) {
    const v = el.getAttribute(attr);
    if (v) {
      push('testid', { testid: v });
      break;
    }
  }

  const role = el.getAttribute('role') || implicitRole(el);
  const name = accessibleName(el, !editable);
  if (role && name) push('role_name', { role, name });

  const ariaLabel = el.getAttribute('aria-label');
  const labelText = associatedLabelText(el);
  if (ariaLabel?.trim()) push('label', { label: ariaLabel.trim() });
  else if (labelText) push('label', { label: labelText });

  // Text locator only for non-editable elements whose visible text identifies them (links, buttons).
  if (!editable && (['a', 'button'].includes(el.tagName.toLowerCase()) || role === 'button' || role === 'link')) {
    const text = (el.textContent || '').trim().replace(/\s+/g, ' ');
    if (text && text.length <= 80) push('text', { text });
  }

  push('css', { css: cssPath(el) });
  push('xpath', { xpath: xpathOf(el) });

  // De-dupe by (strategy + serialized locator), preserving rank order.
  const seen = new Set<string>();
  return out.filter((c) => {
    const k = c.strategy + ':' + JSON.stringify(c.locator);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

// ---------------------------------------------------------------------------------------------------
// Event assembly (applies redaction)
// ---------------------------------------------------------------------------------------------------

/** Build the RecorderEvent for a captured DOM event. Redaction is enforced HERE: a secret field's
 * literal value is dropped and replaced by secretRef — it never reaches the event or the wire. */
export function buildRecorderEvent(
  type: RecorderEventType,
  el: Element,
  rawValue: string | undefined,
  url: string,
): RecorderEvent {
  const event: RecorderEvent = { type, url, selectorCandidates: buildSelectorCandidates(el) };

  if (type === 'input' || type === 'change') {
    const cls = classifyField(el);
    if (cls.secret) {
      event.secretRef = cls.secretRef; // redacted: env ref, never the literal value
    } else {
      // A <select> change must replay as selectOption, not fill() (Playwright rejects fill on a select).
      if (controlKind(el) === 'select') event.verb = 'select';
      if (rawValue !== undefined) event.value = rawValue;
    }
  }
  return event;
}
