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
from brain.state import base_origin_of, normalize_url, page_identity, semantic_id  # noqa: E402

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
    """`canonical_plan_hash` без того, что кодирует КАТАЛОГ ЧЕКАУТА.

    ⚠ ЗАЧЕМ, И ПОЧЕМУ ОДНОЙ ЗАМЕНЫ ПУТИ МАЛО. Сырой `canonical_plan_hash` хеширует все поля всех
    шагов (`brain/state.py`), а путь до фикстуры сидит в них ДВАЖДЫ:
      1. в `intent`/`locator` первого шага — это `navigate` на `file://<абсолютный путь>`;
      2. в `semantic_id` КАЖДОГО шага — `sha1(f"{path}|{role}|{name}")[:12]` (`state.py`).
    Первое чинится подстановкой, второе — нет: это уже посчитанный хеш, и заменить в нём текст
    нельзя. Замерено дважды: при переезде фикстуры из рабочего дерева в репозиторий и затем в CI,
    где чекаут лежит по `/home/runner/work/...`. Оба раза совпали ВСЕ семь наблюдаемых чисел
    (40 · 0.5067 · seen 75 · exercised 39 · navigate 0 · pages 1 · reason None) и разошёлся только
    хеш. То есть гейт в этом виде не мог пройти в CI НИКОГДА.

    Поэтому `semantic_id` из сравнения убран, а путь заменён меткой. Утверждение при этом не
    слабеет: `semantic_id` — чистая функция от `(path, role, name)`, и все три сравниваются и так —
    путь нормализованным URL первого шага, роль и имя локатором. Проверено мутацией: переименование
    раздела, который реально в шагах, гейт по-прежнему роняет.
    """
    stripped = [{k: v for k, v in step.items() if k != "semantic_id"} for step in steps]
    payload = json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.replace(_REPO, "<REPO>").encode()).hexdigest()


LIVE = {
    # ⚠ ПЕРЕЗАМЕРЕНО 2026-08-30 НА НАСТОЯЩЕМ CHROMIUM после ADR-137 (знаменатель покрытия и отсечка
    # кандидата переехали на ось КОНТРОЛА: «эта кнопка», без маршрута, на котором она видна). Три
    # двери прогнаны заново через `bin/agentctl run --planner heuristic`, и `plan_hash_portable`
    # реплики совпал с живым браузером по всем трём — связь, ради которой этот файл существует,
    # цела.
    #
    # ЧТО ВИДНО В САМИХ ЧИСЛАХ. `index` — карта 12 → 19 страниц при том же бюджете в 40 шагов,
    # покрытие 0.2701 → 0.5067, знаменатель 137 → 75. Причина замерена: после ADR-132 путь страницы
    # включает маршрут, поэтому ОДНА кнопка рельса получала двенадцать разных `semantic_id`, и
    # эвристика (`clicks[0]`) честно перебирала их все. Замер до правки: 25 кликов из 38 — рельс,
    # который уже нажимали (66 % бюджета), вырождение начиналось со ВТОРОГО шага, на содержимое
    # экранов оставалось пять шагов. После: повторного рельса НОЛЬ, содержимого 31.
    #
    # ⚠ `navigations` УПАЛИ до нуля, и это не потеря: единственная навигация прежнего прогона уходила
    # на голый адрес двери, а теперь бюджет тратится на содержимое, до которого раньше не доходило.
    #
    # ⚠ `chain` НЕ СДВИНУЛСЯ ВОВСЕ — тот же `plan_hash_portable`, что был. На этой двери общее имя
    # ровно одно (`Start over`) и оно ссылка, то есть вне `_CLICK_ROLES`. Это контроль правки: она
    # меняет ровно то, что обещает, и ничего сверх.
    "index.html": {"steps": 40, "coverage": 0.5067, "seen": 75, "exercised": 39, "navigations": 0,
                   "pages": 19, "reason": None,
                   "plan_hash_portable": "b31100e8ddafb0ed79b221373202f609f379a69cb229b1dcf69f745b0ceaffec"},
    # cards — 40 шагов «сходимости» с покрытием 1.00 по шести элементам превратились в честные 12
    # шагов, 11 элементов и настоящую сходимость: якорь `<a href="#/order/7">`, который фикстура
    # держит приманкой, теперь ведёт в отдельное состояние, и обход по нему уходит ОДИН раз.
    "cards.html": {"steps": 8, "coverage": 1.0, "seen": 6, "exercised": 6, "navigations": 1,
                   "pages": 3, "reason": "converged",
                   "plan_hash_portable": "b8e4b2a1f0fac3b18819576e13b7ff1bf5003e94cf9c69539f88981df0b6f0bb"},
    # chain — было 5 шагов и «нет кандидатов» на одной странице; стало 40 шагов, 11 страниц и упор в
    # потолок. Цепочка чекаута перестала быть тупиком из одного экрана.
    "chain.html": {"steps": 40, "coverage": 0.9444, "seen": 18, "exercised": 17, "navigations": 22,
                   "pages": 11, "reason": None,
                   "plan_hash_portable": "96efd3d884ff3a45fe48d3ab6be903af6cad32610312be9ea95dfac450568b2c"},
}
# Та же цель с поднятым бюджетом (MAX_STEPS=200), замерено живьём в тот же день. Прежде подъём
# бюджета УПИРАЛСЯ в «нет кандидатов» на 53-м шаге — приложение кончалось раньше бюджета. Теперь
# кончается бюджет: 200 шагов, 20 страниц, 213 увиденных элементов. Это и есть то, что делает M3
# утверждением О БЮДЖЕТЕ.
# ⚠ `pages` добавлено 2026-08-24 (ADR-136): рост карты на поднятом бюджете не пиннился ничем, и
# правка, схлопнувшая её обратно, прошла бы мимо всех утверждений этого файла.
LIVE_INDEX_AT_200 = {"steps": 200, "coverage": 0.6667, "reason": None, "pages": 23,
                     "plan_hash_portable": "249ee803a0ea454466f4c2eb09d36ef03acaf2eb55c49850e1f9156773b5c1dd"}

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
  // ADR-135: журнал смен маршрута. Реплика моделирует ПАРУ «браузер + init-скрипт», а не наш
  // скрипт отдельно: снаружи от brain видна ровно одна вещь — что всякая смена адреса без
  // перезагрузки документа попадает в журнал. Как именно она замечена (обёртка history, popstate),
  // проверяет `pw-executor/src/routes.test.ts` на ЖИВОМ Chromium — здесь это было бы утверждением
  // о нашем же коде, переписанным на другом языке.
  //
  // ⚠ ПОТОЛКА ЗДЕСЬ НЕТ НАМЕРЕННО. Он есть у настоящего скрипта и замеряется там; здесь он был бы
  // ВТОРЫМ числом того же смысла, и разойтись эти два числа могли бы молча.
  const journal = [];
  let lastRoute = null;
  function noteRoute(how) {
    if (state.url === lastRoute) return;   // подряд идущий повтор — как в настоящем скрипте
    lastRoute = state.url;
    journal.push({ url: state.url, ts: journal.length + 1, how: how });
  }
  const popHandlers = [];
  const history = {
    pushState: (s, t, u) => { state.url = new URL(u, state.url).href; noteRoute('push'); },
    // Была известна ОДНА функция из двух. `replaceState` — та, которой роутеры делают редиректы, и
    // именно она не оставляет следа ни в истории, ни в снимке адреса: до ADR-135 её не видел никто.
    replaceState: (s, t, u) => { state.url = new URL(u, state.url).href; noteRoute('replace'); },
    back: () => { for (const h of popHandlers) h({}); noteRoute('pop'); },
  };
  const location = {
    get hash() { return new URL(state.url).hash; },
    // ⚠ ЗАМЕРЕНО В ЖИВОМ CHROMIUM: присвоение `location.hash` поднимает `popstate`, поэтому
    // настоящий скрипт видит его как 'pop', а не как отдельный механизм. Реплика повторяет ЗАМЕР,
    // а не спецификацию.
    set hash(v) { state.url = new URL(v, state.url).href; for (const h of popHandlers) h({}); noteRoute('pop'); },
  };
  // Слушатели окна больше не выбрасываются. Прежняя заглушка `() => {}` глотала подписку на
  // `popstate`, которую делает сама фикстура (`testdata/site-spa/app.js`), — то есть реплика
  // расходилась с приложением на возврате назад и молчала об этом.
  const windowObj = {
    addEventListener: (t, h) => { if (t === 'popstate') popHandlers.push(h); },
    document: document,
  };
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
    // ⚠ ФРАГМЕНТ ПЕРЕЖИВАЕТ ЗАГРУЗКУ ДОКУМЕНТА, и до ADR-132 реплике не приходилось это знать:
    // фрагмент отбрасывался ещё в brain, поэтому навигации по нему просто не бывало. Теперь
    // `<a href="#/order/7">` в cards.html попадает во фронтир, обход по нему уходит, и браузер
    // оставляет фрагмент в `location.href` — тогда как `boot()` строит адрес из ОДНОГО пути и
    // фрагмент терял. Замер: с потерей фрагмента реплика давала 40 шагов, 2 страницы и 33 навигации
    // по кругу, живой Chromium — 12 шагов, 3 страницы, сходимость.
    setUrl: (u) => { state.url = u; },
    // Отдать И ОЧИСТИТЬ — как настоящий верб. Журнал, который читают и не чистят, отдавал бы на
    // каждом шаге всё с начала прогона; ворота отсеяли бы это по ADMIT_KNOWN, и утверждение
    // «фронтир пополнился из журнала» осталось бы верным при коде, который не чистит ничего.
    takeRoutes: () => { const out = journal.slice(); journal.length = 0; return out; },
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
    if (req.m === 'navigate') { const u = new URL(req.url); doorPath = u.pathname; ctx = boot(doorPath); if (u.hash) ctx.setUrl(u.href); res = { url: ctx.url() }; }
    else if (req.m === 'url') res = { url: ctx.url() };
    else if (req.m === 'interactives') res = { elements: ctx.interactives() };
    else if (req.m === 'links') res = { links: ctx.links() };
    else if (req.m === 'click') res = ctx.click(req.locator || {});
    else if (req.m === 'fixture') res = ctx.fixture();
    else if (req.m === 'routes') res = { routes: ctx.takeRoutes(), dropped: 0, journal: true };
    else throw new Error('the DOM host was asked for ' + req.m + ', which it does not model');
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
        if m == "browser.routes":
            return self._rpc("routes")
        if m.startswith("browser."):
            # ⚠ НЕИЗВЕСТНАЯ БРАУЗЕРНАЯ ВЕРБА — ОТКАЗ, А НЕ `return {}`. Прежняя последняя строка
            # делала реплику согласной на что угодно: узел, начавший звать незнакомый верб, получал
            # пустой словарь, `.get(...)` давал пустой список, и ЭТОТ ФАЙЛ — главный замер обхода —
            # оставался зелёным над механизмом, которого в замере нет вовсе. Замерено 2026-08-24:
            # второй источник фронтира был проведён целиком, и четыре гейта обхода не заметили.
            raise AssertionError(
                f"граф зовёт {m}, а реплика исполнителя такого верба не знает — замер перестал "
                f"мерить то, что произошло")
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
                "semantic_id": semantic_id(page_identity(target), "navigate", ""),
                "action_type": "navigate", "target": page_identity(target),
                "locator": None, "alternatives": None, "is_milestone": True}
        st = {"run_id": "spa", "run_mode": "explore", "target_url": target,
              "base_origin": base_origin_of(target),
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
            "completeness": final.get("completeness") or {},
            "terminal_records": len(done),
            "plan_hash": final.get("plan_hash", ""),
            "plan_hash_portable": _portable_hash(final.get("exploration_plan", [])),
            "clicks": list(ex.clicks),
            "errors": list(final.get("errors", [])),
            "unactionable": [f for c, f in logs if c == "plan.unactionable_elements"],
            # ADR-135: второй источник фронтира, СЧИТАННЫЙ ИЗ СОБЫТИЙ ПРОГОНА, а не выведенный из
            # исходника. Два числа, а не одно: «журнал отдал N» без «из них взято M» читается как
            # находка, хотя N записей об уже посещённых адресах — это ноль находок.
            "routes_seen": sum(f.get("seen", 0) for c, f in logs if c == "browser.routes_observed"),
            "routes_admitted": sum(f.get("admitted", 0) for c, f in logs if c == "browser.routes_observed"),
            "route_journal_lost": [f for c, f in logs if c == "browser.route_journal_unavailable"],
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


def test_the_route_journal_is_live_here_and_gives_the_frontier_nothing():
    """ADR-135. Второй источник фронтира РАБОТАЕТ на этой цели — и не даёт ей ни одного адреса.

    ⚠ ЭТО ЗАМЕР, А НЕ ДЕФЕКТ, и он опровергает посылку, с которой волна начиналась. Роутер фикстуры
    (`testdata/site-spa/app.js`) устроен так, что КАЖДЫЙ `pushState` немедленно рисует свой экран:

        function go(to) { state.route = to; …; setUrl(to); render(); }

    Значит журнал маршрутов здесь тождественно равен множеству ПОСЕЩЁННЫХ адресов, и ворота
    (`admit_to_frontier`) отвечают на каждую его запись `known`. Прибавки быть не может по
    устройству цели, а не по слабости механизма.

    ⚠ ПАРА УТВЕРЖДЕНИЙ ОБЯЗАТЕЛЬНА. Одно «взято ноль» удовлетворяется каналом, которого нет вовсе —
    ровно тем состоянием, что было до этого PR, — поэтому первое утверждение требует, чтобы журнал
    ОТДАВАЛ записи, и только второе говорит, что ворота их не пустили. Порознь каждое зелено над
    противоположным дефектом.

    Если `admitted` когда-нибудь станет ненулевым, эта строка обязана быть ПЕРЕПИСАНА с новым
    замером, а не удалена: она держит число, которым волна отчитывается.
    """
    r = _walk("index.html")
    # ⚠ ЗДЕСЬ СТОЯЛ ВЫЗОВ `fail(...)`, КОТОРОГО В ЭТОМ МОДУЛЕ НЕТ (внесено в PR-2, ADR-135). Ветка
    # срабатывает только когда журнал потерян — то есть ровно в диагностическом случае, ради которого
    # и писалась, — и подняла бы NameError вместо названного отказа. Зелёной она была потому, что не
    # исполнялась ни разу. Соседние файлы держат собственный `fail()`; этот пользуется `assert`.
    assert not r["route_journal_lost"], (
        f"журнал маршрутов не прочитан ({r['route_journal_lost'][:1]}) — дальнейшие числа "
        f"описывали бы прогон без второго источника, а не прогон с ним")
    assert r["routes_seen"] >= 10, (
        f"журнал отдал {r['routes_seen']} смен(ы) маршрута за прогон, в котором карта набрала "
        f"{r['pages']} страниц — канал молчит, и следующее утверждение стало бы зелёным просто "
        f"потому, что источника нет")
    assert r["routes_admitted"] == 0, (
        f"журнал дал фронтиру {r['routes_admitted']} новых адрес(ов) — на этой цели их не может "
        f"быть ни одного, потому что каждый pushState здесь ПРИЗЕМЛЯЕТСЯ. Либо цель изменилась, "
        f"либо ворота перестали узнавать посещённое; и то и другое требует перезамера, а не правки "
        f"этого числа")
    print(f"  routes: журнал отдал {r['routes_seen']}, ворота пустили {r['routes_admitted']} "
          f"(на этой цели ноль — по устройству роутера)")


def test_the_frontier_takes_route_anchors_now_and_still_cannot_see_a_pushstate_route():
    """M1, ПЕРЕПИСАН ПОД НОВОЕ СОСТОЯНИЕ (ADR-132), и прежнее утверждение записано здесь целиком,
    потому что оно было верным.

    БЫЛО: «фронтир остаётся ПУСТ, хотя на странице есть якоря» — каждый `<a href="#/…">` отбрасывался
    правилом `nu != path`, поскольку `normalize_url` стирал фрагмент и адрес ссылки совпадал с
    адресом текущей страницы. Восемьдесят объявленных состояний схлопывались в ОДНУ запись карты.

    СТАЛО: фрагмент участвует в идентичности, поэтому такой якорь ведёт в ДРУГОЕ состояние и во
    фронтир попадает. Замерено на живом Chromium: карта сайта 1 → 12 страниц, `seen` 75 → 137.

    ⚠ И ОГРАНИЧЕНИЕ, КОТОРОЕ НИКУДА НЕ ДЕЛОСЬ, — ради него фикстура и построена. Фронтир строится из
    `a[href]`, а эта цель ходит `<button>` + `history.pushState`: маршрут, до которого нет якоря, во
    фронтир не попадёт по-прежнему. Поэтому 12 страниц из 80, а не 80 из 80. Утверждать «SPA виден»
    без этой второй половины значило бы обещать больше, чем сделано."""
    r = _walk("index.html")
    assert len(r["links"]) >= 3, (
        f"the door stopped serving anchors ({r['links']}) — тогда утверждение о фронтире ничего "
        f"не говорит о ПРАВИЛЕ, только о том, что ссылок нет")
    # ⚠ ПРЕЖНЕЕ `assert r["navigations"] >= 1` БЫЛО ВАКУУМНЫМ и объявлено доказательством того,
    # чего не доказывало: единственная навигация этого прогона уходит на ГОЛЫЙ адрес двери
    # (`index.html` без фрагмента), а не на маршрутный якорь `#/orders`, ради которого утверждение
    # заводилось. Удовлетворялось оно и кодом, снова отбрасывающим фрагмент. Теперь требуется, чтобы
    # среди посещённого был ИМЕННО маршрутный якорь.
    assert any("#/" in p for p in r["site_map"]), (
        f"среди {r['pages']} ключей карты нет ни одного маршрутного ({sorted(r['site_map'])[:3]}) — "
        f"фрагмент снова не участвует в идентичности, и схлопывание вернулось")
    assert r["pages"] > 1, (
        f"карта сайта держит {r['pages']} страниц(у) — приложение снова читается как один адрес")

    # ⚠ ПРЕЖНИЙ ПОРОГ `pages < states/2` БЫЛ НЕФАЛЬСИФИЦИРУЕМ, и арифметика это показывает без
    # всякого прогона. Слева — ключи `site_map`, то есть идентичности URL; справа — 80 объявленных
    # состояний, из которых 21 МОДАЛЬНОЕ и адреса не имеет вовсе (`app.js`: `openModal` URL не
    # пишет). Единицы разные. А на дефолтном бюджете `pages <= steps == 40` по построению (за шаг в
    # карту добавляется не более одного ключа), тогда как порог — ровно `80/2 = 40`: покраснеть
    # строка могла бы только если КАЖДЫЙ из сорока шагов открывает ни разу не виденное состояние.
    #
    # Переписано на ПОДНЯТЫЙ бюджет и на долю от АДРЕСУЕМЫХ состояний, выведенных из самой фикстуры
    # (`inv["routes"]`, их 59). При 200 шагах `pages` может дойти до 200, то есть утверждение стало
    # фальсифицируемым. Замерено: 20/59 = 0.34.
    inv = r["inventory"]
    addressable = len(inv["routes"])
    assert addressable >= 40, (
        f"фикстура объявляет {addressable} адресуемых маршрутов — знаменатель выведен из неё самой, "
        f"и на таком числе доля ниже ничего не измеряет")
    lifted = _walk("index.html", max_steps=200)
    share = lifted["pages"] / addressable
    assert share < 0.5, (
        f"на бюджете 200 карта держит {lifted['pages']} из {addressable} АДРЕСУЕМЫХ состояний "
        f"({share:.2f}) — если это перестало быть меньшинством, ограничение «маршрут за кнопкой, до "
        f"которой обход не дошёл, ненаходим» исчезло, и переписывать надо ЭТУ строку, с замером")
    assert lifted["reason"] is None and lifted["steps"] == 200, (
        f"на бюджете 200 обход кончился по причине {lifted['reason']!r} на шаге {lifted['steps']} — "
        f"он перестал упираться в бюджет, и доля выше говорит уже о другом механизме")


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
    assert (lifted["steps"], lifted["coverage"], lifted["reason"], lifted["pages"]) == (
        LIVE_INDEX_AT_200["steps"], LIVE_INDEX_AT_200["coverage"], LIVE_INDEX_AT_200["reason"],
        LIVE_INDEX_AT_200["pages"]), (
        f"the lifted-budget run drifted from the live measurement {LIVE_INDEX_AT_200}: {_brief(lifted)}")


def test_the_budget_stop_says_nothing_in_the_transcript_and_says_it_in_the_artefact():
    """M3, ПЕРЕПИСАН ДВАЖДЫ, и оба раза по делу.

    ИСХОДНОЕ УТВЕРЖДЕНИЕ (верное тогда): `plan()` — единственное место, пишущее терминальную запись
    `decision: done` + `reason`, а `route_checkpoint` уводит в `scenario`, как только
    `current_step >= max_steps`. На бюджетном пути `plan()` не достигается, и прогон кончался
    БЕЗ ПРИЧИНЫ, записанной где бы то ни было: ни «max_steps», ничего.

    ПЕРВАЯ ПОЛОВИНА ЭТОГО ВСЁ ЕЩЁ ВЕРНА и утверждается: транскрипт на бюджетном пути по-прежнему пуст.

    ВТОРАЯ ПОЛОВИНА ИСПРАВЛЕНА ADR-131, и это ровно то, ради чего он писался: причина выводится из тех
    же чисел, по которым её распознал бы `plan()`, и лежит в `plan.json` блоком `completeness`. То
    есть «нигде» превратилось в «в артефакте, который человек и открывает».

    Обе половины здесь, потому что порознь каждая обманчива: пустой транскрипт без второй читается
    как дефект, а `completeness` без первой скрывает, что канал остался разным."""
    r = _walk("index.html")
    assert r["terminal_records"] == 0, (
        f"бюджетный путь теперь пишет {r['terminal_records']} терминальную запись "
        f"(reason={r['reason']!r}). Если это сделано намеренно — переписать ЭТУ строку с причиной")
    c = r["completeness"]
    assert c.get("reason") == "max_steps" and c.get("complete") is False, (
        f"артефакт бюджетного прогона не называет причину: {c} — это и есть тот случай, ради "
        f"которого ADR-131 завёл блок, и молчание тут возвращает прежний дефект")
    assert c.get("stopped_at_step") == r["steps"] and c.get("max_steps") == DEFAULT_MAX_STEPS, (
        f"числа блока не совпадают с прогоном: {c} против {r['steps']} шагов")
    # Встречное: сошедшийся прогон запись ДЕЛАЕТ, поэтому пустота выше — про этот путь, а не про
    # неподключённый транскрипт.
    assert _walk("cards.html")["terminal_records"] == 1, "сошедшийся прогон обязан записать причину"


def test_twelve_cards_still_share_one_identity_but_the_run_no_longer_flatters_itself():
    """M2 + M6, ПЕРЕПИСАН ПОД НОВОЕ СОСТОЯНИЕ (ADR-132). Дедуп остался, лесть ушла.

    ДЕДУП — свойство ДОСТУПНЫХ ИМЁН, и правка его не касалась: двенадцать карточек на ОДНОМ маршруте
    носят одно имя «Open», значит один `semantic_id`, значит одну строку в знаменателе покрытия. Это
    по-прежнему так, и утверждается ниже.

    ЛЕСТЬ — свойство ИДЕНТИЧНОСТИ СТРАНИЦЫ, и её правка убрала. БЫЛО: 40 шагов, `coverage 1.00` по
    ШЕСТИ элементам одного адреса и ярлык «converged» на приложении, которого обход почти не видел.
    СТАЛО (замерено живьём): 12 шагов, знаменатель 11, карта 3 страницы, сходимость наступает после
    того, как обход открыл карточку и ВЕРНУЛСЯ. Единица исчезла не потому, что стало хуже, а потому
    что знаменатель перестал быть шестёркой."""
    r = _walk("cards.html")
    assert r["reason"] == "converged", f"ожидалась сходимость, получено {r['reason']!r}"

    # ⚠ ЗДЕСЬ СТОЯЛО `coverage < 1.0`, И ЭТА УЛИКА СТАЛА ЛОЖНОЙ (ADR-137). Единица использовалась как
    # ПРИЗНАК схлопывания маршрутов: рассуждение было «знаменатель считается по одному адресу ⇒
    # покрытие вырождается в 1.0». После переезда знаменателя на ось КОНТРОЛА единица означает
    # ровно противоположное — шесть контролов двери проработаны все шесть, — а маршруты при этом
    # различаются, что утверждается строкой ниже. Косвенный признак заменён на прямой: спрашиваем у
    # карты, помнит ли она РАЗНЫЕ маршруты, вместо того чтобы гадать об этом по числу покрытия.
    routes = {k.split("cards.html")[-1] for k in r["site_map"]}
    assert len({x for x in routes if x.startswith("#/")}) >= 2, (
        f"карта помнит маршруты {sorted(routes)} — якорь `#/order/7`, который фикстура держит "
        f"приманкой, снова ведёт «туда же», и схлопывание маршрутов вернулось")
    assert r["pages"] >= 2, (
        f"карта сайта держит {r['pages']} страниц — якорь `#/order/7`, который фикстура держит "
        f"приманкой, снова ведёт «туда же»")

    # ⚠ И ЛЕСТЬ НИКУДА НЕ ДЕЛАСЬ — она ПРИКРЕПЛЕНА здесь числом, а не подразумевается. Дверь
    # объявляет четырнадцать достижимых человеком состояний, обход трогает шесть контролов и
    # рапортует «сходимость» с покрытием 1.00. Это и есть механизм M6, ради которого фикстура
    # заведена: покрытие честно отвечает на свой вопрос («сколько из увиденного проработано») и
    # ровно поэтому ничего не говорит о приложении. Утверждение стоит затем, чтобы следующий, кто
    # захочет «починить» единицу раздуванием знаменателя, покраснел здесь и прочитал причину.
    assert r["seen"] < 10, (
        f"знаменатель этой двери вырос до {r['seen']} — лесть, ради которой фикстура заведена, "
        f"исчезла; если это осознанная правка, перепишите ЭТУ строку с замером")

    # Дедуп — через ПРОДУКТОВУЮ функцию идентичности над тем, что отдал исполнитель.
    #
    # ⚠ Ключ карты ВЫВОДИТСЯ, а не пишется: до ADR-132 он был голым путём двери, теперь это маршрут
    # (`…cards.html#/orders`), и рукописная строка здесь протухла бы на первой же смене стартового
    # маршрута фикстуры. Берётся страница, на которой карточки и лежат.
    pages_with_cards = {k: v for k, v in r["site_map"].items()
                        if sum(1 for e in v if e["role"] == "button" and e["name"] == "Open") >= 10}
    assert pages_with_cards, (
        f"ни на одной странице карты нет десятка кнопок «Open»: "
        f"{ {k: len(v) for k, v in r['site_map'].items()} } — каталог стал не тем, чем был")
    for path, elements in pages_with_cards.items():
        opens = [e for e in elements if e["role"] == "button" and e["name"] == "Open"]
        assert {e["semantic_id"] for e in opens} == {semantic_id(path, "button", "Open")}, (
            f"на {path} карточки перестали схлопываться в ОДНУ идентичность, которую считает "
            f"brain/state.py для (path, button, 'Open') — покрытие считает их порознь, и фикстура "
            f"больше не меряет дедуп")
    # ⚠ И ЧЕСТНОЕ СЛЕДСТВИЕ, КОТОРОЕ ВИДНО В ЗАМЕРЕ: таких страниц ДВЕ, и на обеих одни и те же
    # двенадцать карточек. Приложение этой фикстуры маршрут из хеша НЕ читает (стартовый берётся из
    # `body[data-start]`), поэтому переход по якорю `#/order/7` меняет адрес и не меняет разметку —
    # и один и тот же экран попадает в карту под двумя идентичностями. Это свойство ФИКСТУРЫ, а не
    # обхода: у настоящего роутера (Angular, Juice Shop) hashchange перерисовывает экран. Сказано
    # здесь, чтобы читатель карты не принял вторую запись за найденный новый экран.
    assert len(pages_with_cards) >= 1, "unreachable — оставлено ради формы утверждения выше"
    opened = [c for c in r["clicks"] if c == "Open"]
    assert len(opened) == 1, f"обход открыл {len(opened)} карточек; их двенадцать, достижима одна"


def test_the_checkout_chain_now_walks_both_branches_and_dies_of_the_budget():
    """M4, ПЕРЕПИСАН ПОД НОВОЕ СОСТОЯНИЕ (ADR-132), и это самое крупное изменение из четырёх.

    БЫЛО: точка ветвления, взята ОДНА ветка, тупик в её конце — и назад дороги нет: верба `back` в
    инструменте не существует, фронтир пуст, кандидаты берутся только с ТЕКУЩЕГО экрана. Обход
    останавливался на 5 шагах из 40 с причиной «нет кандидатов», не увидев ветку счетов вовсе.

    СТАЛО (замерено живьём): 40 шагов, 22 навигации, 11 страниц карты — и в списке кликов есть
    «Pay by invoice». Возврат появился не из ниоткуда: базовый адрес двери — отдельное состояние от
    любого `#/…`-маршрута, он попадает во фронтир, и обход по нему возвращается к точке ветвления.
    Тупик перестал быть тупиком.

    ⚠ ЧТО ЭТО СТОИТ: обход теперь умирает от ПОТОЛКА, а не от «идти некуда». Это ХУДШЕЕ окончание с
    точки зрения читателя — «кончился бюджет» не говорит, сколько осталось, — и потому именно здесь
    ADR-131 требует блока `completeness`. Он утверждается ниже: цена улучшения записана рядом с ним."""
    r = _walk("chain.html")
    clicks = [(c or "").lower() for c in r["clicks"]]
    assert any("invoice" in c for c in clicks), (
        f"ветка счетов снова недостижима: {r['clicks']} — возврат к точке ветвления пропал")
    assert any("card" in c for c in clicks), f"ни одна ветка не взята: {r['clicks']}"
    assert r["pages"] > 1, f"карта держит {r['pages']} страниц — цепочка снова читается как один экран"
    assert r["steps"] == DEFAULT_MAX_STEPS, (
        f"обход прошёл {r['steps']} шагов вместо потолка {DEFAULT_MAX_STEPS} — он снова кончается "
        f"раньше бюджета, и утверждение о цене улучшения меряет не то")
    # Цена, названная своим словом В АРТЕФАКТЕ. Без этого «40 шагов» неотличимы от «прошли всё».
    c = r["completeness"]
    assert c.get("complete") is False and c.get("reason") == "max_steps", (
        f"обход упёрся в потолок и не объявил этого: {c}")
    assert r["unactionable"] == [], (
        f"обход сообщил о непроходимых элементах {r['unactionable']} — молчание здесь и есть находка: "
        f"`plan.unactionable_elements` срабатывает только когда что-то ЗАБЛОКИРОВАНО")


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
