/* ============================================================================
 * Sentinel SPA fixture — the application under test.
 *
 * A classic (non-module) script on purpose: `type="module"` is fetched under CORS
 * rules and a `file://` document has origin `null`, so a module would never load.
 * MEASURED in this environment, headless Chromium: a plain `<script src>` and a
 * `<link rel=stylesheet>` from the same directory both load from `file://`.
 *
 * WHAT THIS FIXTURE IS FOR — the short version; the long one is README.md:
 * every navigation here is a `<button>` + `history.pushState`, never an `<a href>`,
 * so the crawler's navigation frontier (built from `a[href]` only) stays EMPTY and
 * the whole application collapses onto ONE normalized URL path.
 *
 * ROUTING — measured, not assumed. In headless Chromium on `file://`:
 *   history.pushState({}, '', '#/orders')  -> OK,  URL becomes  …/index.html#/orders
 *   history.pushState({}, '', 'orders')    -> SecurityError: "A history state object
 *                                             with URL '…/orders' cannot be created in
 *                                             a document with origin 'null'"
 * So the fragment form of pushState — the one this fixture needs — works, and the
 * path-changing form is what is impossible here. `pushState` fires no event, so the
 * router renders explicitly after it, exactly like a real SPA router. `location.hash`
 * is the fallback, and which one ran is PUBLISHED in the footer rather than assumed.
 * ==========================================================================*/
(function () {
  'use strict';

  /* ---------------------------------------------------------------------
   * 1. THE APPLICATION MODEL
   * Views and transitions are DECLARED here once and everything else is
   * DERIVED from this table: the rendering, the counters in the footer, the
   * floor check. Nothing counts states by hand (docs/DEVELOPMENT.md §0.5).
   * ------------------------------------------------------------------- */

  var ROUTES = {};   // '#/route' -> {title, blurb, kind, controls, extra}
  var MODALS = {};   // 'modal-id' -> {title, blurb, controls}

  function route(id, title, blurb, controls, extra) {
    ROUTES[id] = {
      title: title, blurb: blurb, controls: controls || [],
      kind: (extra && extra.kind) || 'plain', extra: extra || {}
    };
  }
  function modal(id, title, blurb, controls) {
    MODALS[id] = { title: title, blurb: blurb, controls: controls };
  }

  /* --- the section rail: eight buttons, present in EVERY view of the full app.
   * Buttons, not links, and that is the point: `<a href="#/orders">` would be
   * role=link — the crawler proposes clicks for roles {button, tab} only, so an
   * anchor-based rail would leave the walk stuck on the first screen. */
  var RAIL = [
    { label: 'Dashboard',         route: '#/dashboard' },
    { label: 'Orders',            route: '#/orders' },
    { label: 'Customers',         route: '#/customers' },
    { label: 'Inventory',         route: '#/inventory' },
    { label: 'Reports',           route: '#/reports' },
    { label: 'Billing',           route: '#/billing' },
    { label: 'Settings',          route: '#/settings' },
    { label: 'Onboarding wizard', route: '#/wizard/1' }
  ];

  /* --- home ---------------------------------------------------------------- */
  route('#/home', 'Acme Operations', 'The entry screen of the single-page app.', [
    { label: 'Resume last order', route: '#/order/3' },
    { label: 'Open quick actions', modal: 'quick' },
    { label: 'Read release notes', route: '#/notes' }
  ]);
  modal('quick', 'Quick actions', 'A modal state: the URL did not change at all.', [
    { label: 'Close quick actions', close: true }
  ]);
  route('#/notes', 'Release notes', 'Static content page inside the SPA.', [
    { label: 'Back to home from notes', route: '#/home' }
  ]);

  /* --- six generic sections, generated: 4 ways out, 3 sub-views, 1 modal ---- */
  var GENERIC = [
    { label: 'Dashboard',  slug: 'dashboard' },
    { label: 'Customers',  slug: 'customers' },
    { label: 'Inventory',  slug: 'inventory' },
    { label: 'Reports',    slug: 'reports', kind: 'tabs' },
    { label: 'Billing',    slug: 'billing' },
    { label: 'Settings',   slug: 'settings', kind: 'settings' }
  ];
  GENERIC.forEach(function (s) {
    var base = '#/' + s.slug;
    var out = [
      { label: 'Open ' + s.label + ' detail', route: base + '/detail' },
      { label: 'Run ' + s.label + ' check', route: base + '/check' },
      { label: 'Export ' + s.label, route: base + '/export' },
      { label: 'Review ' + s.label, modal: 'review-' + s.slug }
    ];
    if (s.slug === 'billing') out.push({ label: 'Start checkout', route: '#/checkout/pick' });
    route(base, s.label, 'Section screen. Reached from the rail — and only from the rail.',
      out, { kind: s.kind || 'plain' });
    ['detail', 'check', 'export'].forEach(function (sub) {
      route(base + '/' + sub, s.label + ' — ' + sub, 'Sub-view of ' + s.label + '.', [
        { label: 'Back to ' + s.label + ' from ' + sub, route: base }
      ]);
    });
    modal('review-' + s.slug, s.label + ' review',
      'A modal state of ' + s.label + '. The URL is unchanged: this state has no address at all.', [
        { label: 'Close ' + s.label + ' review', close: true }
      ]);
  });

  /* --- Orders: the card branch (M2). Twelve cards, ONE accessible name --------
   * Every card button reads exactly "Open". Same normalized path (the id lives in
   * the fragment, which the crawler strips), same role, same accessible name —
   * therefore ONE semantic_id for all twelve. Do NOT give these buttons a testid
   * or an aria-label carrying the order number: that is the mutation which must
   * make the gate go red, not the fixture's normal state. */
  var CARDS = [];
  for (var ci = 1; ci <= 12; ci++) {
    CARDS.push({ id: ci, ref: 'ORD-10' + (ci < 10 ? '0' + ci : ci), state: (ci % 3 === 0 ? 'unpaid' : 'shipped') });
  }
  // "unpaid", not "open", and that word is load-bearing. `getByRole(role, {name})` matches the
  // name as a case-insensitive SUBSTRING unless `exact` is passed, and the executor resolves with
  // `.first()`. MEASURED here: while this button read "Filter open orders", every click the walk
  // aimed at a card's "Open" landed on the FILTER instead — the crawler marked the card's
  // semantic_id exercised and never left the list. That is a real defect of the locator tier
  // (ADR-082 names the same substring rule on the healing path), and it is deliberately kept OUT
  // of this fixture: a target that measures four convergence mechanisms must not smuggle in a
  // fifth. Do not rename this control back to anything containing "open".
  route('#/orders', 'Orders', 'Catalogue of orders. Twelve cards, one accessible name.', [
    { label: 'Sort orders by date', ui: 'sort' },
    { label: 'Filter unpaid orders', ui: 'filter' }
  ], { kind: 'cards', trailing: [{ label: 'Review Orders', modal: 'review-orders' }] });
  modal('review-orders', 'Orders review', 'Modal state over the catalogue; URL untouched.', [
    { label: 'Close Orders review', close: true }
  ]);
  CARDS.forEach(function (c) {
    route('#/order/' + c.id, 'Order ' + c.ref,
      'Card detail #' + c.id + '. Its own route in the fragment — and the SAME page identity ' +
      'as every other card, because the fragment is stripped before the crawler compares.', [
        { label: 'Back to order list', route: '#/orders' }
      ]);
  });

  /* --- the checkout chain (M4): a branch point, a dead end, no way back ------ */
  route('#/checkout/pick', 'Checkout — payment method',
    'THE BRANCH POINT. Two ways out; the walk can take exactly one of them, and nothing ' +
    'in the tool can bring it back here.', [
      { label: 'Pay with card', route: '#/checkout/card/1' },
      { label: 'Pay by invoice', route: '#/checkout/invoice/1' }
    ]);
  route('#/checkout/card/1', 'Checkout — card', 'Card branch, step 1.', [
    { label: 'Enter card details', route: '#/checkout/card/2' }
  ]);
  route('#/checkout/card/2', 'Checkout — card details', 'Card branch, step 2.', [
    { label: 'Confirm payment', modal: 'charge' }
  ]);
  modal('charge', 'Confirm the charge', 'Modal state; the URL is the same as the screen behind it.', [
    { label: 'Confirm charge', route: '#/checkout/receipt' },
    { label: 'Cancel charge', close: true }
  ]);
  route('#/checkout/receipt', 'Checkout — receipt',
    'THE DEAD END: no button, no tab, nothing to click. Not disabled, not hidden — simply absent, ' +
    'so nothing is "blocked" and the run ends without a word of diagnostics.', [],
    { kind: 'deadend' });
  route('#/checkout/invoice/1', 'Checkout — invoice', 'Invoice branch, step 1 (the road not taken).', [
    { label: 'Enter billing address', route: '#/checkout/invoice/2' }
  ]);
  route('#/checkout/invoice/2', 'Checkout — billing address', 'Invoice branch, step 2.', [
    { label: 'Send invoice', route: '#/checkout/invoice/sent' }
  ]);
  route('#/checkout/invoice/sent', 'Checkout — invoice sent', 'Invoice branch, done.', [
    { label: 'Back to billing', route: '#/billing' }
  ]);

  /* --- the onboarding wizard (M3): the long linear corridor ------------------
   * Three transitions per step (open review -> close review -> continue), so the
   * corridor is longer than any flat step budget the tool currently has. */
  var WIZARD_STEPS = 12;
  for (var k = 1; k <= WIZARD_STEPS; k++) {
    var next = (k < WIZARD_STEPS)
      ? { label: 'Continue to step ' + (k + 1), route: '#/wizard/' + (k + 1) }
      : { label: 'Finish onboarding', route: '#/wizard/done' };
    route('#/wizard/' + k, 'Onboarding — step ' + k + ' of ' + WIZARD_STEPS,
      'One step of a linear corridor. Every label is unique, so nothing here collapses.', [
        { label: 'Review step ' + k, modal: 'wizard-' + k },
        next
      ]);
    modal('wizard-' + k, 'Review of step ' + k, 'Modal state inside the corridor; URL unchanged.', [
      { label: 'Close review ' + k, close: true }
    ]);
  }
  route('#/wizard/done', 'Onboarding complete', 'End of the corridor.', [
    { label: 'Back to home', route: '#/home' }
  ]);

  /* ---------------------------------------------------------------------
   * 2. DERIVED INVENTORY (+ the floor that a derivation always needs)
   * ------------------------------------------------------------------- */

  // The DOM-ordered control list of a route. ONE function, used by the renderer
  // AND by the counter — a second list would be the hand-kept inventory §0.5 bans.
  function controlsOf(id) {
    var v = ROUTES[id];
    if (!v) return [];
    var out = v.controls.slice();
    if (v.kind === 'cards') {
      CARDS.forEach(function (c) { out.push({ label: 'Open', route: '#/order/' + c.id, card: c }); });
      (v.extra.trailing || []).forEach(function (c) { out.push(c); });
    }
    if (v.kind === 'tabs') {
      ['Summary', 'Breakdown', 'Exports'].forEach(function (t, i) {
        out.push({ label: t + ' tab', tab: i, role: 'tab' });
      });
    }
    return out;
  }

  var ROUTE_IDS = Object.keys(ROUTES);
  var MODAL_IDS = Object.keys(MODALS);
  var STATES = ROUTE_IDS.length + MODAL_IDS.length;
  var TRANSITIONS = RAIL.length
    + ROUTE_IDS.reduce(function (n, id) { return n + controlsOf(id).length; }, 0)
    + MODAL_IDS.reduce(function (n, id) { return n + MODALS[id].controls.length; }, 0);

  // The floor. A derived count that nobody bounds passes perfectly over an empty
  // set — the one failure mode a derivation cannot catch by itself.
  var FLOOR_STATES = 50, FLOOR_TRANSITIONS = 60;
  var UNDERSIZED = (STATES < FLOOR_STATES) || (TRANSITIONS < FLOOR_TRANSITIONS);

  /* ---------------------------------------------------------------------
   * 3. THE ROUTER — pushState, never an anchor
   * ------------------------------------------------------------------- */

  var body = document.body;
  var RAIL_ON = body.getAttribute('data-rail') !== 'off';
  var START = body.getAttribute('data-start') || '#/home';
  var state = { route: START, modal: null, ui: 'default', tab: 0 };
  var routerMode = 'unknown';   // measured on the first navigation, published in the footer

  function setUrl(to) {
    try {
      history.pushState({ route: to }, '', to);
      routerMode = 'history.pushState';
    } catch (e) {
      // Recorded, not swallowed: which mechanism moved the URL is a fact about the
      // fixture, and a fixture that quietly changed mechanism would explain nothing.
      routerMode = 'location.hash (pushState refused: ' + e.name + ')';
      location.hash = to;
    }
  }

  function go(to) { state.route = to; state.modal = null; state.tab = 0; setUrl(to); render(); }
  function openModal(id) { state.modal = id; render(); }        // no URL write, on purpose
  function closeModal() { state.modal = null; render(); }        // no URL write, on purpose
  function setUi(mode) { state.ui = (state.ui === mode ? 'default' : mode); render(); }
  function setTab(i) { state.tab = i; render(); }

  /* ---------------------------------------------------------------------
   * 4. RENDERING
   * ------------------------------------------------------------------- */

  var live = [];   // controls of the CURRENT screen, indexed by data-ctl

  function button(c, i, cls) {
    var b = document.createElement('button');
    b.type = 'button';
    if (c.role === 'tab') {
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(c.tab === state.tab));
    }
    if (cls) b.className = cls;
    b.setAttribute('data-ctl', String(i));
    b.textContent = c.label;
    return b;
  }

  function renderRail(host) {
    if (!RAIL_ON) return;
    var nav = document.createElement('nav');
    nav.setAttribute('aria-label', 'sections');
    RAIL.forEach(function (r) {
      var b = document.createElement('button');
      b.type = 'button';
      b.setAttribute('data-rail-to', r.route);
      // Section membership, not route equality: step 11 of the wizard is still the
      // wizard. `aria-current` changes no accessible name, so the walk is unaffected.
      var prefix = r.route.split('/').slice(0, 2).join('/');
      if (state.route === prefix || state.route.indexOf(prefix + '/') === 0)
        b.setAttribute('aria-current', 'page');
      b.textContent = r.label;
      nav.appendChild(b);
    });
    host.appendChild(nav);
  }

  function renderCards(main, controls, offset) {
    var order = CARDS.slice();
    if (state.ui === 'sort') order.reverse();
    var ul = document.createElement('ul');
    ul.className = 'cards';
    ul.setAttribute('aria-label', 'orders');
    order.forEach(function (c) {
      if (state.ui === 'filter' && c.state !== 'unpaid') return;
      var i = -1;
      controls.forEach(function (ctl, n) { if (ctl.card === c) i = n; });
      var li = document.createElement('li');
      var h = document.createElement('h3');
      h.textContent = c.ref;
      var p = document.createElement('p');
      p.textContent = 'state: ' + c.state;
      li.appendChild(h); li.appendChild(p);
      li.appendChild(button(controls[i], i));   // accessible name: "Open" — for all twelve
      ul.appendChild(li);
    });
    main.appendChild(ul);
  }

  function renderTabs(main, controls) {
    var list = document.createElement('div');
    list.setAttribute('role', 'tablist');
    list.setAttribute('aria-label', 'report sections');
    var panel = document.createElement('div');
    panel.setAttribute('role', 'tabpanel');
    controls.forEach(function (c, i) {
      if (c.role !== 'tab') return;
      list.appendChild(button(c, i));
      if (c.tab === state.tab) panel.textContent = 'Panel: ' + c.label + '. Switching tabs changes no URL.';
    });
    main.appendChild(list);
    main.appendChild(panel);
  }

  function renderSettingsForm(main) {
    // Roles textbox / combobox / checkbox: perceived, mapped, and NOT part of the
    // coverage denominator — explore proposes clicks for {button, tab} only.
    var f = document.createElement('form');
    f.className = 'settings';
    f.setAttribute('aria-label', 'settings');
    f.innerHTML =
      '<label for="s-name">Workspace name</label><input id="s-name" name="name" type="text" value="Acme">' +
      '<label for="s-region">Region</label><select id="s-region" name="region">' +
      '<option>eu-central</option><option>us-east</option></select>' +
      '<label for="s-beta"><input id="s-beta" name="beta" type="checkbox"> Join the beta channel</label>';
    main.appendChild(f);
  }

  function render() {
    var v = ROUTES[state.route];
    if (!v) { state.route = START; v = ROUTES[START]; }
    var m = state.modal ? MODALS[state.modal] : null;
    live = m ? m.controls.slice() : controlsOf(state.route);

    var head = document.getElementById('app-head');
    head.innerHTML = '';
    var h1 = document.createElement('h1');
    h1.textContent = 'Acme Operations';      // the APP, once; the screen names itself in <h2>
    var sub = document.createElement('p');
    sub.className = 'subtitle';
    sub.textContent = m ? m.blurb : v.blurb;
    head.appendChild(h1); head.appendChild(sub);

    var rail = document.getElementById('app-rail');
    rail.innerHTML = '';
    renderRail(rail);

    var main = document.getElementById('view');
    main.innerHTML = '';
    var r = document.createElement('p');
    r.className = 'route';
    r.textContent = 'route ' + state.route + (m ? '  ·  modal "' + state.modal + '" (no URL of its own)' : '')
      + (state.ui !== 'default' ? '  ·  list state "' + state.ui + '" (no URL of its own)' : '');
    main.appendChild(r);

    if (m) {
      // The screen underneath is drawn as INERT TEXT while a modal is up. A live
      // overlay would make the crawler fight Playwright's actionability check
      // instead of its own convergence, and this fixture measures convergence.
      var under = document.createElement('div');
      under.className = 'inert-under';
      under.textContent = 'Behind the dialog: ' + v.title + ' (' + state.route + ') — inert while the dialog is open.';
      main.appendChild(under);
      var dlg = document.createElement('div');
      dlg.setAttribute('role', 'dialog');
      dlg.setAttribute('aria-modal', 'true');
      dlg.setAttribute('aria-label', m.title);
      var h2 = document.createElement('h2');
      h2.textContent = m.title;
      dlg.appendChild(h2);
      var box = document.createElement('div');
      box.className = 'controls';
      m.controls.forEach(function (c, i) { box.appendChild(button(c, i, i ? 'secondary' : null)); });
      dlg.appendChild(box);
      main.appendChild(dlg);
    } else {
      var h2b = document.createElement('h2');
      h2b.textContent = v.title;
      main.appendChild(h2b);
      var box2 = document.createElement('div');
      box2.className = 'controls';
      live.forEach(function (c, i) {
        if (c.card || c.role === 'tab') return;    // rendered by their own sections below
        box2.appendChild(button(c, i));
      });
      main.appendChild(box2);
      if (v.kind === 'cards') renderCards(main, live, 0);
      if (v.kind === 'tabs') renderTabs(main, live);
      if (v.kind === 'settings') renderSettingsForm(main);
      if (v.kind === 'deadend') {
        var d = document.createElement('p');
        d.textContent = 'Nothing here can be clicked. This is where the walk stops.';
        main.appendChild(d);
      }
    }

    document.title = 'SPA fixture · ' + (m ? m.title : v.title) + ' · ' + STATES + ' states · '
      + TRANSITIONS + ' transitions' + (UNDERSIZED ? ' · FIXTURE-UNDERSIZED' : '');

    var out = document.getElementById('derived-counts');
    if (out) {
      out.textContent = STATES + ' states (' + ROUTE_IDS.length + ' routes + ' + MODAL_IDS.length
        + ' modal states), floor ' + FLOOR_STATES + ' · ' + TRANSITIONS + ' transitions, floor '
        + FLOOR_TRANSITIONS + ' · ' + (UNDERSIZED ? 'BELOW FLOOR — the fixture is broken' : 'above floor')
        + ' · router: ' + routerMode;
    }
  }

  /* ---------------------------------------------------------------------
   * 5. ONE delegated listener — the DOM is replaced on every transition
   * ------------------------------------------------------------------- */
  document.addEventListener('click', function (ev) {
    var el = ev.target.closest ? ev.target.closest('button') : null;
    if (!el) return;
    var railTo = el.getAttribute('data-rail-to');
    if (railTo) { go(railTo); return; }
    var idx = el.getAttribute('data-ctl');
    if (idx === null) return;
    var c = live[Number(idx)];
    if (!c) return;
    if (c.close) { closeModal(); return; }
    if (c.modal) { openModal(c.modal); return; }
    if (c.ui) { setUi(c.ui); return; }
    if (c.role === 'tab') { setTab(c.tab); return; }
    if (c.route) { go(c.route); return; }
  });

  // A pushState router owns the back button too; there is no `back` verb in the
  // tool, so this exists for a human driving the fixture by hand.
  window.addEventListener('popstate', function (e) {
    state.route = (e.state && e.state.route) || START;
    state.modal = null;
    render();
  });

  /* ---------------------------------------------------------------------
   * 6. A READ-ONLY SEAM FOR WHOEVER MEASURES THIS FIXTURE
   * A harness that wants the inventory must not keep its own copy of it — that
   * is the hand-kept list docs/DEVELOPMENT.md §0.5 bans, and the copy would go
   * stale silently. So the model publishes itself, derived from the same tables
   * the renderer uses. It is data only: no element, no listener, nothing the
   * crawler's perception can see, so the target under test is unchanged.
   * ------------------------------------------------------------------- */
  window.__spaFixture = {
    states: STATES, transitions: TRANSITIONS,
    floorStates: FLOOR_STATES, floorTransitions: FLOOR_TRANSITIONS, undersized: UNDERSIZED,
    routes: ROUTE_IDS.slice(), modals: MODAL_IDS.slice(), rail: RAIL.map(function (r) { return r.label; }),
    // Every clickable label that can share a screen: the route's own controls plus,
    // when the rail is mounted, the rail. A modal state shows its own controls only.
    labelsOf: function (id) {
      if (MODALS[id]) return MODALS[id].controls.map(function (c) { return c.label; });
      return (RAIL_ON ? RAIL.map(function (r) { return r.label; }) : [])
        .concat(controlsOf(id).map(function (c) { return c.label; }));
    },
    targetsOf: function (id) {
      var cs = MODALS[id] ? MODALS[id].controls : controlsOf(id);
      return cs.map(function (c) {
        return { label: c.label, route: c.route || null, modal: c.modal || null,
                 close: !!c.close, ui: c.ui || null, tab: (c.role === 'tab') };
      });
    }
  };

  // First paint: put the start route in the URL through the SAME router the
  // buttons use, so `routerMode` is measured before it is reported.
  setUrl(START);
  render();
})();
