"""Offline gate: keep the crawl measurement honest (PROD-CRAWL) and MEASURE CONVERGENCE on the SPA
fixture (WF-SPA-FIXTURE, testdata/site-spa).

Run:  .venv/bin/python tests/test_crawl_measured_offline.py

PART 1 (PROD-CRAWL, unchanged). docs/CRAWL_ANALYSIS.internal.md rests on measured facts about the
coverage model. The load-bearing one is the set of roles coverage is computed over — the whole
"stale vs real" verdict on the buttons-only claim hinges on it being exactly button+tab. That value
is imported, not grepped, so widening the coverage roles fails here and forces the measurement to be
re-taken rather than silently drifting.

PART 2 (WF-SPA-FIXTURE). What the walk does on an application it cannot get around. testdata/site
has 4 pages and 12 transitions, the walk covers all of it, and every gate over it is green — so
nothing in this suite could say what happens when the target is bigger than the tool. site-spa is
that target: one normalized URL, 80 declared states, 139 declared transitions, and four separate
mechanisms that stop the walk. The claims made here are OUTCOMES OF A RUN, never the shape of
brain/graph.py: an assertion about source text is a surrogate — mutations walk straight through it —
so every number below comes out of the real explore graph, the real HeuristicPlanner and the real
coverage model exercising the real fixture.

WHY THE BROWSER IS REPLACED, AND WHAT KEEPS THAT HONEST. A real run needs Chromium, a Playwright
download and ~40 real clicks; this suite must stay a `python tests/*.py` with no browser and no
network. So the fixture's OWN app.js runs inside a DOM small enough to read (_HOST_JS below, ~110
lines of node), and the graph drives it through the same `browser.*` calls pw-executor answers.

That is a replica, and a replica is worth exactly what it is measured against. It is bound to the
live run at the sharpest point available: `canonical_plan_hash` over the frozen steps. The three
doors reproduce the hashes of the real-Chromium runs BYTE FOR BYTE (see LIVE below), which means
every one of the 40 / 7 / 5 steps — intent, semantic_id, locator, alternatives — is the step the
real browser produced. When the replica stops being faithful, that equality is what goes red, and
the answer is to re-measure against Chromium rather than to adjust the number here.

FLOORS. A derived count nobody bounds passes perfectly over an empty set, so the inventory carries
floors, and they sit just BELOW what was measured (80 states -> 70, 139 transitions -> 120): a floor
above everything ever seen only fires when the fixture is deleted.
"""
import atexit
import hashlib
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.checkpoint.memory import MemorySaver     # noqa: E402

from brain import budget, graph as graph_mod            # noqa: E402
from brain.graph import _CLICK_ROLES, build_graph       # noqa: E402
from brain.planner import HeuristicPlanner              # noqa: E402
from brain.state import normalize_url, semantic_id      # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO, "testdata", "site-spa")

# The product's own default exploration budget: `cmd/agentctl/main.go` (-max-steps "40") and
# `brain/__main__.py` (MAX_STEPS env, default 40) — the live runs took it unchanged. It is asserted
# rather than trusted: `test_the_flat_budget_...` runs with NO max_steps in the init state, so the
# graph falls back to its own default and the walk's length MEASURES that number.
DEFAULT_MAX_STEPS = 40

# Floors on the fixture's declared size, just under the measured 80 / 139. They exist because the
# inventory below is DERIVED (from the fixture's own tables, via its read-only seam) and a derivation
# that nobody bounds reports a perfect zero over an empty set.
FLOOR_STATES = 70
FLOOR_TRANSITIONS = 120
# How many times bigger the application must be than one flat budget for M3 to be about the budget
# and not about a small page. Measured 139/40 = 3.47.
FLOOR_TRANSITIONS_PER_BUDGET = 3

# THE LIVE MEASUREMENT THIS FILE IS BOUND TO — real headless Chromium over file://, no network, no
# docker, no ollama, 2026-08-18:
#
#   ./bin/agentctl run --target "file://$PWD/testdata/site-spa/<door>" --planner heuristic
#
# run ids: index 0580031cc844162e (and 73f33161b7a51528, an identical second run), cards
# 04d1b1ef55874742, chain cdebdca97309f5ac. Those run dirs are gitignored — which is exactly why the
# numbers are written down HERE, in the check that uses them, instead of being read from artifacts
# that a `runs/` cleanup can take away. `reason` is the terminal transcript record's reason, and
# `None` means the run wrote NO terminal record at all (see the budget test).
def _portable_hash(steps: "list") -> str:
    """`canonical_plan_hash`, но НЕ зависящий от места чекаута.

    ⚠ ЗАЧЕМ. `canonical_plan_hash` хеширует ВСЕ поля всех шагов (`brain/state.py:96`), а шаг №1 —
    это `navigate` на `file://<абсолютный путь>/testdata/site-spa/…`. Значит сырой хеш кодирует
    каталог, в котором лежит репозиторий. Замерено: фикстура переехала из рабочего дерева агента в
    репозиторий — все семь наблюдаемых чисел совпали (40 шагов, coverage 0.5067, seen 75,
    exercised 39, navigations 0, pages 1, reason None), а хеш разошёлся. В CI, где чекаут лежит по
    `/home/runner/work/…`, этот гейт покраснел бы у КАЖДОГО прогона, и краснел бы не по делу.

    Выбрасывать хеш нельзя — он и есть та проверка, которая доказывает, что offline-реплика
    воспроизводит настоящий Chromium пошагово. Поэтому путь репозитория заменяется меткой ДО
    хеширования, с обеих сторон сравнения: утверждение сохраняется целиком, а переносимость
    появляется.
    """
    payload = json.dumps(steps, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.replace(_REPO, "<REPO>").encode()).hexdigest()


LIVE = {
    "index.html": {"steps": 40, "coverage": 0.5067, "seen": 75, "exercised": 39, "navigations": 0,
                   "pages": 1, "reason": None,
                   "plan_hash_portable": "53d6d44ad541007c3d6fa61f1a79654ad60b7ead3ec783978e0c778138351556"},
    "cards.html": {"steps": 7, "coverage": 1.0, "seen": 6, "exercised": 6, "navigations": 0,
                   "pages": 1, "reason": "converged",
                   "plan_hash_portable": "2e1869689ddd3c2de74cf775b5fae4662bad92c66d9b1ed2760f0317829ff28f"},
    "chain.html": {"steps": 5, "coverage": 0.6667, "seen": 6, "exercised": 4, "navigations": 0,
                   "pages": 1, "reason": "no_candidates",
                   "plan_hash_portable": "75311e97b10a33c727eaf1d0801b35645a7c9c0814b5883ac565022245bd31ac"},
}
# The same target with the budget lifted (MAX_STEPS=200), measured live the same day: the walk goes
# further and dies of something else. This is what makes M3 a statement about the BUDGET.
LIVE_INDEX_AT_200 = {"steps": 53, "coverage": 0.6341, "reason": "no_candidates"}

# --- the DOM the fixture runs in ------------------------------------------------------------------
# Small on purpose, and generic: it knows about elements, attributes, text and one delegated click
# listener, and nothing whatsoever about routes, cards or wizards — those live in the fixture's app.js,
# which is the code under measurement here. What it deliberately does NOT model: `innerHTML` parsing
# (so the settings form's inputs are absent), boxes and computed styles (everything reports visible),
# and Playwright's accessible-name engine (a button's name is its text). None of those touch a number
# asserted below — coverage counts button/tab only — and the plan_hash equality with the live runs is
# what proves it rather than this comment.
_HOST_JS = r"""
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const readline = require('readline');

let doorPath = process.argv[2];
let ctx = null;

function El(tag) { this.tagName = tag; this.childNodes = []; this.attrs = {}; this.parentNode = null; this._text = ''; this._html = ''; }
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
El.prototype.appendChild = function (c) { c.parentNode = this; this.childNodes.push(c); return c; };
El.prototype.closest = function (sel) { let n = this; while (n) { if (n.tagName === sel) return n; n = n.parentNode; } return null; };
Object.defineProperty(El.prototype, 'textContent', {
  get() { return this.childNodes.length ? this.childNodes.map(c => c.textContent).join('') : this._text; },
  set(v) { this._text = String(v); this.childNodes = []; }
});
Object.defineProperty(El.prototype, 'innerHTML', {
  get() { return this._html; },
  set(v) { this.childNodes = []; this._html = String(v); }
});

function boot(door) {
  const html = fs.readFileSync(door, 'utf8');
  const appJs = fs.readFileSync(path.join(path.dirname(door), 'app.js'), 'utf8');
  const url0 = new URL('file://' + door).href;

  const body = new El('body');
  const battr = /<body([^>]*)>/.exec(html);
  if (battr) { const re = /([a-zA-Z-]+)="([^"]*)"/g; let m; while ((m = re.exec(battr[1]))) body.setAttribute(m[1], m[2]); }

  const appHead = new El('header'), appRail = new El('div'), view = new El('main'), counts = new El('span');
  const byId = { 'app-head': appHead, 'app-rail': appRail, 'view': view, 'derived-counts': counts };

  // The document's own anchors (the footer decoys) — app.js never creates one.
  const anchors = [];
  const are = /<a\s+href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/g; let a;
  while ((a = are.exec(html))) {
    const el = new El('a');
    el.setAttribute('href', new URL(a[1], url0).href);
    el.textContent = a[2].replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
    anchors.push(el);
  }

  const clickHandlers = [];
  const state = { url: url0 };
  const document = {
    body: body,
    createElement: (t) => new El(t),
    getElementById: (id) => byId[id] || null,
    addEventListener: (t, h) => { if (t === 'click') clickHandlers.push(h); },
    title: ''
  };
  const history = { pushState: (s, t, u) => { state.url = new URL(u, state.url).href; } };
  const location = { get hash() { return new URL(state.url).hash; }, set hash(v) { state.url = new URL(v, state.url).href; } };
  const windowObj = { addEventListener: () => {}, document: document };
  const sandbox = { document, history, location, window: windowObj, console };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(appJs, sandbox, { filename: door });

  function walk(n, out) { for (const c of n.childNodes) { out.push(c); walk(c, out); } return out; }
  function harvest() {
    const nodes = [];
    for (const root of [appHead, appRail, view]) walk(root, nodes);   // document order: head, rail, view,
    for (const el of anchors) nodes.push(el);                          // then the footer
    const out = [];
    for (const e of nodes) {
      const roleAttr = e.getAttribute('role');
      const isCtl = e.tagName === 'button' || roleAttr === 'button' || roleAttr === 'tab' ||
                    (e.tagName === 'a' && e.getAttribute('href'));
      if (!isCtl) continue;
      const role = roleAttr || (e.tagName === 'button' ? 'button' : 'link');
      const text = String(e.textContent || '').replace(/\s+/g, ' ').trim();
      const name = (e.getAttribute('aria-label') || text).replace(/\s+/g, ' ').trim();
      out.push({ el: e, d: { role, name, testid: e.getAttribute('data-testid'), text,
                             tag: e.tagName, visible: true, disabled: false } });
    }
    return out;
  }
  return {
    url: () => state.url,
    interactives: () => harvest().map(h => h.d),
    links: () => anchors.map(e => ({ href: e.getAttribute('href'), text: String(e.textContent).trim() })),
    click: (loc) => {
      // getByRole(role, {name}).first(): the name matches as a case-insensitive SUBSTRING (no
      // `exact`), and `.first()` takes the first match in document order — pw-executor's buildLocator.
      const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
      const want = norm(loc.name);
      const hit = harvest().filter(h => h.d.role === loc.role && norm(h.d.name).includes(want))[0];
      if (!hit) throw new Error('no element matched ' + JSON.stringify(loc));
      for (const h of clickHandlers) h({ target: hit.el });
      return { clicked: true, url: state.url };
    },
    // The fixture's read-only seam: its inventory, derived by the fixture from the same tables its
    // renderer uses. Read, never re-counted here — a second count would be the hand-kept list
    // docs/DEVELOPMENT.md §0.5 bans.
    fixture: () => {
      const f = windowObj.__spaFixture || {};
      return { states: f.states, transitions: f.transitions, floorStates: f.floorStates,
               floorTransitions: f.floorTransitions, undersized: f.undersized,
               routes: f.routes || [], modals: f.modals || [], rail: f.rail || [] };
    }
  };
}

ctx = boot(doorPath);
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  let res;
  try {
    const req = JSON.parse(line);
    if (req.m === 'navigate') { doorPath = new URL(req.url).pathname; ctx = boot(doorPath); res = { url: ctx.url() }; }
    else if (req.m === 'url') res = { url: ctx.url() };
    else if (req.m === 'interactives') res = { elements: ctx.interactives() };
    else if (req.m === 'links') res = { links: ctx.links() };
    else if (req.m === 'click') res = ctx.click(req.locator || {});
    else if (req.m === 'fixture') res = ctx.fixture();
    else res = {};
    process.stdout.write(JSON.stringify({ ok: true, r: res }) + '\n');
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, e: String((e && e.message) || e) }) + '\n');
  }
});
"""

_host_js_path = None


def _host_js() -> str:
    """The host, materialised once per process — and removed on exit, so a suite run leaves nothing
    behind but its exit code."""
    global _host_js_path
    if _host_js_path is None:
        d = tempfile.mkdtemp(prefix="spa-host-")
        _host_js_path = os.path.join(d, "spa_dom_host.js")
        with open(_host_js_path, "w") as fh:
            fh.write(_HOST_JS)
        atexit.register(shutil.rmtree, d, True)
    return _host_js_path


class FixtureEx:
    """The pw-executor surface the explore graph uses, answered by the fixture running in _HOST_JS."""

    def __init__(self, door: str):
        assert shutil.which("node") is not None, (
            "node is not on PATH, so the convergence measurement cannot be taken. This is a FAILURE, "
            "not a skip: a check that quietly reports success without running is the vacuous pass this "
            "repo keeps finding in its own tests. Install node (the same one pw-executor needs).")
        self.p = subprocess.Popen(["node", _host_js(), door], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, bufsize=1)
        self.navigations, self.clicks = [], []

    def _rpc(self, m, **kw):
        self.p.stdin.write(json.dumps(dict(m=m, **kw)) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError(f"the DOM host died answering {m}")
        r = json.loads(line)
        if not r["ok"]:
            raise RuntimeError(r["e"])
        return r["r"]

    def call(self, m, **p):
        if m == "browser.navigate":
            self.navigations.append(p["url"])
            return self._rpc("navigate", url=p["url"])
        if m == "browser.currentUrl":
            return {"url": self._rpc("url")["url"], "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "", "nodeCount": 0}
        if m == "browser.interactives":
            return self._rpc("interactives")
        if m == "browser.links":
            return self._rpc("links")
        if m == "browser.click":
            self.clicks.append((p.get("locator") or {}).get("name"))
            return self._rpc("click", locator=p["locator"])
        if m == "browser.screenshotHash":
            return {"hash": "h"}
        return {}

    def inventory(self):
        return self._rpc("fixture")

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def _brief(r: dict) -> dict:
    """What a failure message may say about a run. Never the whole site map — a wall of elements in a
    test failure is how an assertion message stops being read."""
    return {k: r[k] for k in ("door", "steps", "coverage", "seen", "exercised", "navigations",
                              "pages", "reason", "terminal_records")}


_walks: dict = {}


def _walk(door: str, max_steps=None) -> dict:
    """Run the REAL explore graph over `door` and return what the run DID. Cached per (door, budget).

    `max_steps=None` leaves the key out of the init state entirely, so the graph falls back to its own
    default — which is how DEFAULT_MAX_STEPS gets measured instead of transcribed.
    """
    key = (door, max_steps)
    if key in _walks:
        return _walks[key]

    path = os.path.join(FIXTURE, door)
    target = "file://" + path
    ex = FixtureEx(path)
    tx, logs = [], []
    art = tempfile.mkdtemp(prefix="spa-run-")
    prev_frames = os.environ.get("SENTINEL_LIVE_FRAMES")
    os.environ["SENTINEL_LIVE_FRAMES"] = "0"     # no PNG per step: nothing here looks at pictures
    orig_log = graph_mod.log
    graph_mod.log = lambda code, **f: logs.append((code, f))
    budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
    try:
        ex.call("browser.navigate", url=target)
        ex.navigations.clear()
        # Byte-identical to brain/__main__.py `_run_explore`, because the plan_hash comparison below
        # only means something if the run starts from the same state the real one did.
        init = {"step_id": 1, "intent": f"navigate to target {target}",
                "semantic_id": semantic_id(normalize_url(target), "navigate", ""),
                "action_type": "navigate", "target": normalize_url(target),
                "locator": None, "alternatives": None, "is_milestone": True}
        st = {"run_id": "spa", "run_mode": "explore", "target_url": target,
              "base_origin": normalize_url(target).rsplit("/", 1)[0] + "/",
              "coverage_target": 0.85, "artifact_dir": art, "goal": "", "describe": "",
              "site_map": {}, "phase": "explore", "scenario_steps": [], "scenario_unmatched": [],
              "current_url": target, "page_model": {}, "exploration_plan": [init], "plan_hash": "",
              "current_step": 1, "interactive_seen": [], "interactive_exercised": [],
              "visited_paths": [], "nav_frontier": [], "coverage_achieved": 0.0,
              "exploration_complete": False,
              "executed_actions": [{"step_id": 1, "type": "navigate", "ok": True}], "errors": []}
        if max_steps is not None:
            st["max_steps"] = max_steps
        app = build_graph(ex, HeuristicPlanner(), tx.append).compile(checkpointer=MemorySaver())
        cfg = {"recursion_limit": max(60, (max_steps or DEFAULT_MAX_STEPS) * 8),
               "configurable": {"thread_id": "spa"}}
        with contextlib.redirect_stdout(io.StringIO()):   # the run's @@AGUI stream is not the subject
            final = app.invoke(st, config=cfg)
        done = [r for r in tx if r.get("decision") == "done"]
        out = {
            "door": door,
            "steps": len(final.get("exploration_plan", [])),
            "coverage": round(final.get("coverage_achieved", 0.0), 4),
            "seen": len(final.get("interactive_seen", [])),
            "exercised": len(final.get("interactive_exercised", [])),
            "navigations": len(ex.navigations),
            "pages": len(final.get("site_map", {}) or {}),
            "site_map": final.get("site_map", {}) or {},
            "frontier": list(final.get("nav_frontier", []) or []),
            "reason": (done[-1].get("reason") if done else None),
            "terminal_records": len(done),
            "plan_hash": final.get("plan_hash", ""),
            "plan_hash_portable": _portable_hash(final.get("exploration_plan", [])),
            "clicks": list(ex.clicks),
            "errors": list(final.get("errors", [])),
            "unactionable": [f for c, f in logs if c == "plan.unactionable_elements"],
            "links": [l["href"] for l in ex.call("browser.links").get("links", [])],
            "inventory": ex.inventory(),
        }
    finally:
        graph_mod.log = orig_log
        if prev_frames is None:
            os.environ.pop("SENTINEL_LIVE_FRAMES", None)
        else:
            os.environ["SENTINEL_LIVE_FRAMES"] = prev_frames
        ex.close()
        shutil.rmtree(art, ignore_errors=True)
    _walks[key] = out
    return out


# --- PART 1: the coverage model (PROD-CRAWL) ------------------------------------------------------
def test_coverage_roles_are_button_and_tab():
    """If this changes, docs/CRAWL_ANALYSIS.internal.md is out of date: the coverage denominator moved,
    and the "buttons-only is stale / links-excluded is by design" verdicts must be re-measured."""
    assert _CLICK_ROLES == ("button", "tab"), (
        f"coverage roles are now {_CLICK_ROLES!r}, not (button, tab) — re-measure the crawl analysis")
    assert all(isinstance(r, str) for r in _CLICK_ROLES), "coverage roles must be role strings"


# --- PART 2: convergence on the SPA fixture (WF-SPA-FIXTURE) --------------------------------------
def test_the_fixture_declares_more_application_than_one_budget_can_walk():
    """The target has to be genuinely bigger than the tool, or every measurement below is about a
    small page. The inventory is the fixture's own derivation (window.__spaFixture), read through the
    seam rather than re-counted here; the floors are this file's, and they sit under what was
    measured (80 states / 139 transitions)."""
    inv = _walk("index.html")["inventory"]
    assert inv["undersized"] is False, f"the fixture reports itself below its OWN floor: {inv}"
    assert inv["states"] >= FLOOR_STATES, (
        f"the fixture declares {inv['states']} states, floor {FLOOR_STATES} — it shrank, so every "
        f"convergence number in this file describes a different application")
    assert inv["transitions"] >= FLOOR_TRANSITIONS, (
        f"the fixture declares {inv['transitions']} transitions, floor {FLOOR_TRANSITIONS}")
    assert inv["transitions"] >= FLOOR_TRANSITIONS_PER_BUDGET * DEFAULT_MAX_STEPS, (
        f"{inv['transitions']} transitions is under {FLOOR_TRANSITIONS_PER_BUDGET}x the default budget "
        f"of {DEFAULT_MAX_STEPS} steps — a walk that fits is not a measurement of a flat budget")
    # The catalogue branch has to keep enough cards for the dedup measurement to mean anything.
    cards = [r for r in inv["routes"] if r.startswith("#/order/")]
    assert len(cards) >= 10, f"only {len(cards)} card routes left; the dedup claim needs a dozen"


def test_the_navigation_frontier_stays_empty_although_the_page_has_anchors():
    """M1. The frontier is built from `a[href]` only, and this application navigates with
    history.pushState behind <button>. The point is not that anchors are absent — the page HAS them,
    and the executor reports them — it is that every one is dropped by the frontier rule, so 80
    declared states collapse onto ONE entry in the site map.

    ⚠ WHERE THIS IS MEASURED IS THE WHOLE CHECK, and it was got wrong first: "the walk never
    navigated" is VACUOUS at the default budget. HeuristicPlanner takes every click before any
    navigate, and this application always has another click, so the walk would never reach a frontier
    entry even if one existed. MEASURED: turning a decoy into a real `<a href="chain.html">` leaves
    navigations at 0 for all 40 steps and only shows up at 200. So the frontier LIST is asserted on
    the default run (that is the rule's own output, budget or no budget) and the NAVIGATION COUNT on
    the lifted-budget run, where every click is exhausted and a live entry would be taken."""
    r = _walk("index.html")
    lifted = _walk("index.html", max_steps=200)
    assert len(r["links"]) >= 3, (
        f"the door stopped serving decoy anchors ({r['links']}) — then an empty frontier proves "
        f"nothing about the RULE, only that the page has no links")
    assert r["frontier"] == [], (
        f"the frontier collected {r['frontier']} — one of the anchors the fixture guarantees is "
        f"dropped now enters it")
    assert (r["navigations"], lifted["navigations"]) == (0, 0), (
        f"the walk navigated: {r['navigations']} at the default budget, {lifted['navigations']} at 200 "
        f"steps with every click exhausted — a frontier entry appeared where the fixture guarantees none")
    assert (r["pages"], lifted["pages"]) == (1, 1), (
        f"site-map holds {r['pages']} / {lifted['pages']} pages; the whole application is one "
        f"normalized path, and it stays one however long the walk runs")
    inv = r["inventory"]
    assert inv["states"] - r["pages"] >= FLOOR_STATES - 1, (
        f"only {inv['states'] - r['pages']} declared states stayed outside the map — the collapse "
        f"this fixture exists to show is gone")


def test_the_flat_budget_ends_the_walk_short_of_its_coverage_target():
    """M3. Run with NO max_steps in the init state: the graph uses its own default, and the length of
    the walk MEASURES that default rather than trusting DEFAULT_MAX_STEPS. The walk stops on the
    budget with the coverage target unreached — and the control run below shows the budget is what
    stopped it."""
    r = _walk("index.html")
    assert r["steps"] == DEFAULT_MAX_STEPS, (
        f"the walk ran {r['steps']} steps against a default budget of {DEFAULT_MAX_STEPS} — either the "
        f"product's default moved or the walk stopped for another reason; re-measure")
    assert r["coverage"] < 0.85, f"coverage {r['coverage']} reached the 0.85 target — nothing stalled"
    assert not r["errors"], f"the walk must stall on the budget, not on broken steps: {r['errors'][:3]}"

    lifted = _walk("index.html", max_steps=200)
    assert lifted["steps"] > r["steps"] and lifted["coverage"] > r["coverage"], (
        f"with the budget at 200 the walk did {lifted['steps']} steps / {lifted['coverage']} coverage "
        f"against {r['steps']} / {r['coverage']} — if raising the budget changes nothing, the walk was "
        f"stopped by something else and this test is measuring the wrong mechanism")
    assert (lifted["steps"], lifted["coverage"], lifted["reason"]) == (
        LIVE_INDEX_AT_200["steps"], LIVE_INDEX_AT_200["coverage"], LIVE_INDEX_AT_200["reason"]), (
        f"the lifted-budget run drifted from the live measurement {LIVE_INDEX_AT_200}: {_brief(lifted)}")


def test_the_budget_stop_writes_no_terminal_record_at_all():
    """M3, the part a reader of the artifacts meets. `plan()` is the ONLY place that writes the
    terminal `decision: done` + `reason` record — and `route_checkpoint` diverts to `scenario` as soon
    as `current_step >= max_steps`, so on the budget path plan() is never reached. The run ends with
    no reason recorded anywhere: not "max_steps", nothing. Measured first on this fixture, because on
    testdata/site the budget is never spent (8 steps of 40)."""
    r = _walk("index.html")
    assert r["terminal_records"] == 0, (
        f"the budget stop now writes {r['terminal_records']} terminal record(s) (reason={r['reason']!r}). "
        f"That is an IMPROVEMENT if it was deliberate — the run finally says why it stopped — and this "
        f"assertion is then what must be rewritten, with the reason recorded, rather than deleted")
    # …and the same run does record a reason when it converges or runs dry, so the emptiness above is
    # about this path and not about the transcript being unwired.
    assert _walk("cards.html")["terminal_records"] == 1, "the converged run must record its reason"
    assert _walk("chain.html")["terminal_records"] == 1, "the exhausted run must record its reason"


def test_twelve_cards_collapse_to_one_identity_and_the_run_calls_that_converged():
    """M2 + M6. Twelve card buttons share one accessible name, so they share ONE semantic_id: one
    entry in the coverage denominator and one in the numerator. The walk opens a single card of
    twelve, reaches coverage 1.00 over the six identities it can see, and the run is labelled
    `converged` — the flattering terminal reason, on an application it walked almost none of."""
    r = _walk("cards.html")
    assert r["reason"] == "converged", f"expected the flattering label, got {r['reason']!r}"
    assert r["coverage"] == 1.0, f"coverage {r['coverage']} — convergence at less than 1.00?"

    # The dedup itself, through the production identity function over what the executor reported.
    # HOW MANY entries the map holds is deliberately not asserted: whether a page's duplicate controls
    # are stored once or twelve times is an unrelated property of the map builder, and a check that
    # rides on it would go red the day that changes for a good reason. What must hold is that they all
    # carry the ONE identity, and that the walk's denominator is far smaller than the catalogue.
    path = normalize_url("file://" + os.path.join(FIXTURE, "cards.html"))
    opens = [e for e in r["site_map"][path] if e["role"] == "button" and e["name"] == "Open"]
    assert opens, "no button named 'Open' reached the site map — the catalogue is not what it was"
    assert {e["semantic_id"] for e in opens} == {semantic_id(path, "button", "Open")}, (
        "the cards no longer collapse to the ONE identity brain/state.py computes for "
        "(path, button, 'Open') — coverage now counts them separately, so this fixture stopped "
        "measuring the dedup it was built for")
    cards = [x for x in r["inventory"]["routes"] if x.startswith("#/order/")]
    assert r["seen"] < len(cards), (
        f"{r['seen']} identities seen against {len(cards)} card routes the fixture declares — the "
        f"denominator grew, and coverage 1.00 would no longer be the small-denominator effect this "
        f"test is about")
    opened = [c for c in r["clicks"] if c == "Open"]
    assert len(opened) == 1, f"the walk opened {len(opened)} cards; twelve exist and one is reachable"


def test_the_checkout_chain_stops_with_budget_left_and_says_nothing():
    """M4. A branch point, one branch taken, a dead end at the end of it — and no way back: there is
    no `back` verb, the frontier is empty, and candidates come only from the CURRENT screen. The walk
    stops with 87% of its budget unspent, under its coverage target, and — because nothing was
    blocked, disabled or hidden — without a single line of diagnostics."""
    r = _walk("chain.html")
    assert r["reason"] == "no_candidates", f"expected no_candidates, got {r['reason']!r}"
    assert r["steps"] < DEFAULT_MAX_STEPS / 2, (
        f"{r['steps']} steps — the point is that the walk stops with budget LEFT, not that it runs out")
    assert r["coverage"] < 0.85, f"coverage {r['coverage']} met the target on a chain it half-walked"
    assert r["unactionable"] == [], (
        f"the run now reports unactionable elements {r['unactionable']} — the silence is the finding: "
        f"`plan.unactionable_elements` only fires when something was BLOCKED, and a dead end blocks "
        f"nothing, so the tester is told nothing at all")
    # The road not taken: the invoice branch is three states the walk can never reach from here.
    assert not any("invoice" in (c or "").lower() for c in r["clicks"]), (
        f"the invoice branch became reachable: {r['clicks']}")
    assert any("card" in (c or "").lower() for c in r["clicks"]), f"no branch was taken at all: {r['clicks']}"


def test_the_offline_replica_still_reproduces_the_live_browser_run():
    """The bind. Everything above runs against a DOM stub; this is what says the stub is still the
    application the real browser walked. `plan_hash` is `canonical_plan_hash` over the frozen steps,
    so equality here means every step — intent, semantic_id, locator, alternatives — matches the live
    Chromium run recorded in LIVE. When this goes red, re-measure against Chromium; do not edit the
    expectation to match the replica."""
    for door, live in LIVE.items():
        r = _walk(door)
        got = {k: r[k] for k in ("steps", "coverage", "seen", "exercised", "navigations", "pages",
                                 "reason", "plan_hash_portable")}
        assert got == live, (
            f"{door}: the offline replica drifted from the live measurement.\n  live: {live}\n  got:  {got}")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}\n       {e}")
        except Exception as e:      # a raise from the graph or the host is a failure, not a crash to hide
            failed += 1
            print(f"  FAIL {t.__name__}\n       {type(e).__name__}: {e}")
    if _walks:
        print(f"\n  coverage roles = {_CLICK_ROLES}")
        for (door, ms), r in _walks.items():
            inv = r["inventory"]
            print(f"  {door:11s} budget={ms or DEFAULT_MAX_STEPS:<4d} steps={r['steps']:<3d} "
                  f"coverage={r['coverage']:<6} seen={r['seen']:<3d} exercised={r['exercised']:<3d} "
                  f"navigate={r['navigations']} site-map={r['pages']} reason={str(r['reason']):<13s} "
                  f"| fixture declares {inv['states']} states / {inv['transitions']} transitions")
    print(f"\ncrawl-measured: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
