/*
 * GiftSense finder UI, loaded by giftsense.js on the shopper's first click.
 * A dialog (bottom sheet on phones): a few quick questions → 3–5 gift picks
 * with reasons → "Show different ideas" (excludes what was shown).
 * Safety: every piece of text goes in through textContent, never parsed as HTML;
 * links are only followed for storefront-relative paths.
 */
(function () {
  if (window.GiftSenseUI) return;

  var MAX_EXCLUDE = 50;
  var AGE_FOR = { kid: 'kid', teen: 'teen', new_baby: 'baby' };
  var state = { answers: { recipient: null, occasion: null, budget_band: null, vibes: [], free_text: '' },
    shown: [], refines: 0, asked: [], added: {} };   // added: product_id -> how it was added (gift / cart)
  var ctx = null;

  // The shopper's answers and ideas are kept in this browser for a few hours,
  // so "Find a gift" in another tab or on the next page carries on where they
  // left off. Stays on the device; nothing here is sent anywhere.
  var SAVE_KEY = 'giftsense:finder';
  var SAVE_TTL = 12 * 3600 * 1000;
  var SAVED = ['answers', 'shown', 'refines', 'asked', 'added', 'lastPicks', 'step', 'view'];
  function save() {
    try {
      var out = { t: Date.now() };
      SAVED.forEach(function (k) { out[k] = state[k]; });
      localStorage.setItem(SAVE_KEY, JSON.stringify(out));
    } catch (e) { /* private mode / storage full: just don't remember */ }
  }
  (function restore() {
    try {
      var saved = JSON.parse(localStorage.getItem(SAVE_KEY) || 'null');
      if (!saved || !(saved.t > Date.now() - SAVE_TTL)) return;
      SAVED.forEach(function (k) { if (saved[k] != null) state[k] = saved[k]; });
    } catch (e) { /* unreadable: start fresh */ }
  })();
  var els = {};

  // ── tiny DOM helpers ──────────────────────────────────────────────────────
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // Inline SVG icons, built with DOM calls (no HTML parsing). Each path is
  // [d, attrs]; strokes use currentColor so CSS sets the color.
  function svg(size, viewBox, paths, cls) {
    var NS = 'http://www.w3.org/2000/svg';
    var s = document.createElementNS(NS, 'svg');
    s.setAttribute('width', size); s.setAttribute('height', size);
    s.setAttribute('viewBox', viewBox); s.setAttribute('aria-hidden', 'true');
    if (cls) s.setAttribute('class', cls);
    paths.forEach(function (p) {
      var n = document.createElementNS(NS, p.tag || 'path');
      Object.keys(p).forEach(function (k) { if (k !== 'tag') n.setAttribute(k, p[k]); });
      s.appendChild(n);
    });
    return s;
  }
  var STROKE = { fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' };
  function withStroke(d) { var o = { d: d }; Object.keys(STROKE).forEach(function (k) { o[k] = STROKE[k]; }); return o; }
  // Gift box (header mark, tinted with the store's accent color).
  function giftIcon() {
    return svg(22, '0 0 24 24', [
      withStroke('M4 11h16v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-9z'),
      withStroke('M3 7h18v4H3z'),
      withStroke('M12 7v14'),
      withStroke('M12 7c-1.5-3-5-3.5-5-1.25C7 7 9.5 7 12 7zm0 0c1.5-3 5-3.5 5-1.25C17 7 14.5 7 12 7z')
    ]);
  }
  function closeIcon() { return svg(16, '0 0 24 24', [withStroke('M6 6l12 12M18 6L6 18')]); }

  // GiftSense / Prudix mark for the "Gift ideas by GiftSense" footer badge.
  function brandMark() {
    return svg(14, '0 0 32 32', [
      { d: 'M15 27 L12 4 L6 4 Q1 4 1 11 Q1 18 6 18 L14 18', fill: 'none', stroke: '#eab308', 'stroke-width': '3', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
      { tag: 'circle', cx: '17.5', cy: '15', r: '1.8', fill: '#eab308' },
      { d: 'M20 19 L28 27 M28 19 L20 27', fill: 'none', stroke: 'currentColor', 'stroke-width': '2.2', 'stroke-linecap': 'round', transform: 'rotate(15, 24, 23)' }
    ], 'gs-badge-mark');
  }

  // Shopper-facing text in the storefront's language: giftsense-i18n-<lang>.js
  // (loaded with the UI) sets window.GiftSenseI18n = { "English": "…" }.
  // Missing keys fall back to English; {name} placeholders are filled in.
  function tr(text, vars) {
    var dict = window.GiftSenseI18n || {};
    var out = dict[text] || text;
    if (vars) Object.keys(vars).forEach(function (k) { out = out.split('{' + k + '}').join(vars[k]); });
    return out;
  }

  function money(n) {
    try {
      return new Intl.NumberFormat(ctx.locale, { style: 'currency', currency: ctx.currency }).format(n);
    } catch (e) { return ctx.currency + ' ' + Number(n).toFixed(2); }
  }

  // Storefront root, including a language/market prefix such as "/fr/".
  function root() {
    var r = window.Shopify && window.Shopify.routes && window.Shopify.routes.root;
    return typeof r === 'string' && r.charAt(0) === '/' ? r : '/';
  }

  // Only storefront-relative paths from our API are followed, prefixed with the root.
  function safeHref(url) {
    if (typeof url !== 'string' || url.charAt(0) !== '/' || url.charAt(1) === '/') return null;
    return root() + url.slice(1);
  }

  // Links used while browsing ideas open in a new tab: results live only in
  // this dialog, so leaving the page would lose them. Screen readers are told.
  function newTab(a, label) {
    a.target = '_blank';
    a.rel = 'noopener';
    a.setAttribute('aria-label', (label || a.textContent) + ' (' + tr('opens in a new tab') + ')');
    return a;
  }

  function handleOf(url) {
    var m = typeof url === 'string' && url.match(/^\/products\/([\w-]+)$/);
    return m ? m[1] : null;
  }

  // ── add to cart ───────────────────────────────────────────────────────────
  // Variants come from the theme's own /products/<handle>.js. One variant →
  // add it here; several (sizes, colors) → send the shopper to the product
  // page. Hidden `_giftsense_*` line properties and cart attribute let the
  // order be attributed to the gift finder (never customer data).
  // A tile whose product is already in the cart says so (it survives going to
  // the gift panel and back, and reopening the dialog).
  // 'gift': in the cart as a gift. 'cart': in the cart as a plain item (added
  // here, or from the product page): offer to make it a gift.
  function addedState(pick, slot) {
    var how = state.added[pick.product_id];
    if (how === 'gift') {
      var b = el('button', 'gs-secondary gs-small gs-added', tr('Added as a gift \u2713'));
      b.type = 'button';
      b.disabled = true;
      slot.appendChild(b);
    } else {
      var make = el('button', 'gs-primary gs-small', tr('Make it a gift'));
      make.type = 'button';
      make.addEventListener('click', function () { makeItAGift(pick, make); });
      slot.appendChild(make);
      slot.appendChild(el('span', 'gs-in-cart', tr('In your cart \u2713')));
    }
    var view = el('a', 'gs-link', tr('View cart'));
    view.href = root() + 'cart';
    slot.appendChild(newTab(view));
  }

  // Gift panel for a pick that's already in the cart as a plain item: it uses
  // the variant they chose (size, color…) and swaps that line for the gift one.
  function makeItAGift(pick, button) {
    button.disabled = true;
    fetch(root() + 'cart.js', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (cart) {
      var line = plainLine(cart, pick.product_id);
      if (!line) { delete state.added[pick.product_id]; save(); renderResults(state.lastPicks || [], false, true); return; }
      renderGiftPanel(pick, line.variant_id, { convert: true });
    }).catch(function () { button.disabled = false; });
  }

  function plainLine(cart, productId) {
    return ((cart && cart.items) || []).filter(function (it) {
      var props = it.properties || {};
      return String(it.product_id) === String(productId) && !props._giftsense_gift && !props['Gift for'] &&
        !props._giftsense_wrap_for && !props._giftsense_card_for;
    })[0] || null;
  }

  // Tiles reflect the live cart (another tab, the product page, a removal):
  // re-read it and redraw the ideas if anything changed.
  function syncCart() {
    var picks = state.lastPicks || [];
    if (!picks.length) return;
    fetch(root() + 'cart.js', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (cart) {
      var before = JSON.stringify(state.added);
      var added = {};
      picks.forEach(function (p) {
        var lines = ((cart && cart.items) || []).filter(function (it) { return String(it.product_id) === String(p.product_id); });
        if (!lines.length) return;
        added[p.product_id] = lines.some(function (it) { return (it.properties || {})._giftsense_gift; }) ? 'gift' : 'cart';
      });
      state.added = added;
      save();
      if (JSON.stringify(added) !== before && els.overlay && !els.overlay.hidden && els.body.querySelector('.gs-results')) {
        renderResults(picks, false, true);
      }
    }).catch(function () { /* keep what we had */ });
  }
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && els.overlay && !els.overlay.hidden) syncCart();
  });

  function cartButton(pick, slot) {
    if (state.added[pick.product_id]) { addedState(pick, slot); return; }
    var handle = handleOf(pick.url);
    if (!handle) return;
    fetch(root() + 'products/' + handle + '.js', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (product) {
        if (!product || !product.variants || !slot.isConnected) return;
        var available = product.variants.filter(function (v) { return v.available; });
        if (!available.length) {
          var sold = el('button', 'gs-secondary gs-small', tr('Sold out'));
          sold.type = 'button';
          sold.disabled = true;
          slot.appendChild(sold);
          return;
        }
        if (product.variants.length > 1) {
          var choose = el('a', 'gs-secondary gs-small', tr('Choose options'));
          choose.href = safeHref(pick.url);
          newTab(choose);
          slot.appendChild(choose);
          return;
        }
        var gift = el('button', 'gs-primary gs-small', tr('Add as a gift'));
        gift.type = 'button';
        gift.addEventListener('click', function () { renderGiftPanel(pick, available[0].id); });
        slot.appendChild(gift);
        // Secondary, in words: a plain "Add to cart" under the main action.
        var add = el('button', 'gs-text-btn', tr('Add to cart'));
        add.type = 'button';
        add.addEventListener('click', function () { addToCart(available[0].id, add, slot, pick.product_id); });
        slot.appendChild(add);
      })
      .catch(function () { /* the card link still works */ });
  }

  // ── analytics beacon (POST /apps/giftsense/events) ───────────────────────
  // Batched; flushed every 3 s and when the page is hidden (keepalive), so
  // closing the tab doesn't lose the last events. Never blocks the UI.
  var queue = [];
  var flushTimer = null;
  function track(type, productId) {
    if (!ctx) return;
    queue.push(productId ? { type: type, product_id: String(productId).slice(0, 40) } : { type: type });
    if (queue.length >= 20) flush();
    else if (!flushTimer) flushTimer = setTimeout(flush, 3000);
  }
  function flush() {
    clearTimeout(flushTimer);
    flushTimer = null;
    if (!queue.length || !ctx) return;
    var batch = queue.splice(0, 20);
    try {
      fetch(ctx.api + '/events', {
        method: 'POST', credentials: 'same-origin', keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sid: ctx.sid, events: batch })
      }).catch(function () {});
    } catch (e) { /* analytics must never break the widget */ }
  }
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flush(); });
  window.addEventListener('pagehide', flush);

  function addToCart(variantId, button, slot, productId) {
    button.disabled = true;
    button.textContent = tr('Adding\u2026');
    var json = { 'Content-Type': 'application/json', Accept: 'application/json' };
    fetch(root() + 'cart/add.js', {
      method: 'POST', credentials: 'same-origin', headers: json,
      body: JSON.stringify({ items: [{ id: variantId, quantity: 1,
        properties: { _giftsense_gift: '1' } }] })
    }).then(function (r) {
      if (!r.ok) throw new Error('add');
      return fetch(root() + 'cart/update.js', {
        method: 'POST', credentials: 'same-origin', headers: json,
        body: JSON.stringify({ attributes: { _giftsense_sid: ctx.sid } })
      });
    }).then(function () {
      track('pick_atc', productId);
      if (productId) {
        state.added[productId] = state.added[productId] || 'cart';
        save();
        clear(slot);
        addedState({ product_id: productId }, slot);
      } else {
        button.textContent = tr('Added \u2713');
      }
      document.dispatchEvent(new CustomEvent('giftsense:added-to-cart', { detail: { variantId: variantId } }));
    }).catch(function () {
      button.disabled = false;
      button.textContent = tr('Try again');
    });
  }

  function sized(url, width) {
    if (typeof url !== 'string' || url.indexOf('https://') !== 0) return null;
    return url + (url.indexOf('?') === -1 ? '?' : '&') + 'width=' + width;
  }

  // ── dialog shell ──────────────────────────────────────────────────────────
  function build() {
    els.overlay = el('div', 'gs-overlay');
    // On phones the sheet fills most of the screen and a stray tap above it is
    // easy; close there only with ✕ (or Esc). Wider screens: backdrop closes too.
    els.overlay.addEventListener('click', function (e) {
      if (e.target === els.overlay && !(window.matchMedia && matchMedia('(max-width: 640px)').matches)) close();
    });
    els.dialog = el('div', 'gs-dialog');
    els.dialog.setAttribute('role', 'dialog');
    els.dialog.setAttribute('aria-modal', 'true');
    els.dialog.setAttribute('aria-labelledby', 'gs-title');

    var head = el('div', 'gs-head');
    var mark = el('span', 'gs-mark');
    mark.appendChild(giftIcon());
    var heading = el('div', 'gs-heading');
    var title = el('h2', 'gs-title', tr('Find the perfect gift'));
    title.id = 'gs-title';
    els.subtitle = el('p', 'gs-subtitle');
    heading.appendChild(title);
    heading.appendChild(els.subtitle);
    var x = el('button', 'gs-close');
    x.type = 'button';
    x.setAttribute('aria-label', tr('Close'));
    x.appendChild(closeIcon());
    x.addEventListener('click', close);
    head.appendChild(mark);
    head.appendChild(heading);
    head.appendChild(x);

    els.body = el('div', 'gs-body');
    els.foot = el('div', 'gs-foot');
    els.dialog.appendChild(head);
    els.dialog.appendChild(els.body);
    els.dialog.appendChild(els.foot);
    els.overlay.appendChild(els.dialog);
    document.body.appendChild(els.overlay);
    document.addEventListener('keydown', onKey);
  }

  function onKey(e) {
    if (!els.overlay || els.overlay.hidden) return;
    if (e.key === 'Escape') { close(); return; }
    if (e.key !== 'Tab') return;
    var f = els.dialog.querySelectorAll('button:not([disabled]), a[href], textarea, [tabindex="0"]');
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
    else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
  }

  function open(context) {
    ctx = context;
    if (!els.overlay) build();
    els.dialog.style.setProperty('--gs-accent', (ctx.accent || '').trim() || '#111827');
    els.dialog.style.setProperty('--gs-on-accent', (ctx.onAccent || '').trim() || '#ffffff');
    els.overlay.hidden = false;
    document.documentElement.classList.add('gs-lock');
    var finder = !(ctx.entry && ctx.entry.type !== 'finder');
    document.getElementById('gs-title').textContent = finder ? tr('Find the perfect gift') : tr('Gift options');
    els.subtitle.textContent = finder ? tr('Answer a few quick questions and get ideas picked for them')
      : tr('Add a gift note, wrapping and more');
    var entry = ctx.entry;
    if (entry && entry.type === 'product' && entry.product && entry.product.variant_id) {
      renderGiftPanel({ product_id: entry.product.product_id, title: entry.product.title, wrap: entry.product.wrap },
                      Number(entry.product.variant_id), { askContext: true, standalone: true });
    } else if (entry && entry.type === 'cart') {
      renderCartNote();
    } else if (state.view === 'results' && state.lastPicks && state.lastPicks.length) {
      renderResults(state.lastPicks, false, true);   // closed, or a new tab? the ideas are still here
      syncCart();                                    // …marked with what's in the cart now
    } else {
      renderIntake();
    }
    track(entry && entry.type === 'product' ? 'panel_open' : 'widget_open');
  }

  function close() {
    if (!els.overlay) return;
    els.overlay.hidden = true;
    document.documentElement.classList.remove('gs-lock');
    if (ctx && ctx.trigger) ctx.trigger.focus();
  }

  function renderFoot() {
    clear(els.foot);
    if (!ctx.config.show_badge) return;
    var badge = el('span', 'gs-badge');
    badge.appendChild(brandMark());
    badge.appendChild(el('span', null, tr('Gift ideas by GiftSense')));
    els.foot.appendChild(badge);
  }

  // ── step 1: questions ─────────────────────────────────────────────────────
  // One question per screen (fits a phone without scrolling): a progress bar,
  // the question's chips, Back / Next. Picking a single-choice answer moves on
  // by itself; the last step (vibes + optional note) shows "Show gift ideas".
  function chipGroup(label, options, key, multi, max, onPick) {
    var wrap = el('fieldset', 'gs-group');
    wrap.appendChild(el('legend', 'gs-label', label));
    var row = el('div', 'gs-chips');
    var buttons = [];
    function sync() {
      buttons.forEach(function (b) {
        var v = b.getAttribute('data-value');
        var on = multi ? state.answers[key].indexOf(v) !== -1 : state.answers[key] === v;
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      updateGo();
    }
    options.forEach(function (o) {
      var b = el('button', 'gs-chip', tr(o.label));
      b.type = 'button';
      b.setAttribute('data-value', o.value);
      b.addEventListener('click', function () {
        if (multi) {
          var list = state.answers[key];
          var i = list.indexOf(o.value);
          if (i !== -1) list.splice(i, 1);
          else { list.push(o.value); if (list.length > max) list.shift(); }
        } else {
          state.answers[key] = o.value;
        }
        sync();   // update in place: no re-render, so scroll and focus stay put
        if (onPick) onPick();
      });
      buttons.push(b);
      row.appendChild(b);
    });
    wrap.appendChild(row);
    sync();
    return wrap;
  }

  function ready() {
    var a = state.answers;
    return !!(a.recipient && a.occasion && a.budget_band);
  }

  function updateGo() {
    if (!els.go) return;
    var step = steps()[state.step || 0];
    els.go.disabled = step.last ? !ready() : !state.answers[step.key];
    save();
  }

  function steps() {
    var intake = ctx.config.intake;
    return [
      { key: 'recipient', label: tr('Who is it for?'), options: intake.recipients },
      { key: 'occasion', label: tr("What's the occasion?"), options: intake.occasions },
      { key: 'budget_band', label: tr("What's your budget?"), options: intake.budgets },
      { key: 'vibes', label: tr('They are\u2026 (pick up to {n})', { n: intake.max_vibes }), options: intake.vibes,
        multi: true, max: intake.max_vibes, last: true }
    ];
  }

  function goTo(n) {
    state.step = n;
    renderIntake();
    save();
  }

  function renderIntake() {
    var intake = ctx.config.intake;
    var all = steps();
    var n = Math.min(state.step || 0, all.length - 1);
    // Can't skip ahead past an unanswered required question.
    for (var k = 0; k < n; k++) if (!all[k].last && !state.answers[all[k].key]) { n = k; break; }
    state.step = n;
    state.view = 'intake';
    var step = all[n];
    clear(els.body);
    els.go = null;

    var progress = el('div', 'gs-progress');
    progress.appendChild(el('span', 'gs-progress-text', tr('Step {n} of {total}', { n: n + 1, total: all.length })));
    var bar = el('div', 'gs-progress-bar');
    var fill = el('span', 'gs-progress-fill');
    fill.style.width = (100 * (n + 1) / all.length) + '%';
    bar.appendChild(fill);
    progress.appendChild(bar);
    els.body.appendChild(progress);

    var autoNext = step.last ? null : function () {
      var seq = state.seq = (state.seq || 0) + 1;
      setTimeout(function () { if (state.seq === seq && state.step === n) goTo(n + 1); }, 180);
    };
    els.body.appendChild(chipGroup(step.label, step.options, step.key, step.multi, step.max, autoNext));

    if (step.last) {
      var noteWrap = el('label', 'gs-group');
      noteWrap.appendChild(el('span', 'gs-label', tr('Anything else? (optional)')));
      var note = el('textarea', 'gs-note');
      note.rows = 2;
      note.maxLength = intake.max_free_text || 200;
      note.placeholder = tr('e.g. loves hiking and strong coffee');
      note.value = state.answers.free_text;
      note.addEventListener('input', function () { state.answers.free_text = note.value; });
      noteWrap.appendChild(note);
      els.body.appendChild(noteWrap);
    }

    var nav = el('div', 'gs-nav');
    if (n > 0) {
      var back = el('button', 'gs-secondary', tr('Back'));
      back.type = 'button';
      back.addEventListener('click', function () { goTo(n - 1); });
      nav.appendChild(back);
    }
    els.go = el('button', 'gs-primary', step.last ? tr('Show gift ideas') : tr('Next'));
    els.go.type = 'button';
    els.go.addEventListener('click', function () {
      if (!step.last) { goTo(n + 1); return; }
      state.shown = []; state.refines = 0; state.asked = [];
      track('intake_complete');
      search();
    });
    nav.appendChild(els.go);
    els.body.appendChild(nav);
    updateGo();
    renderFoot();
    els.body.scrollTop = 0;
    var first = els.body.querySelector('.gs-chip[aria-pressed="true"]') || els.body.querySelector('.gs-chip');
    if (first) first.focus();
  }

  // ── step 2: results ───────────────────────────────────────────────────────
  // Loading state while the AI picks (a few seconds): a short line of text
  // that moves on, with a small animated indicator. No empty placeholder boxes.
  var LOADING_STEPS = [tr('Looking through the store\u2026'), tr('Picking the best matches\u2026'), tr('Writing why each one fits\u2026')];
  function skeleton() {
    clear(els.body);
    clearInterval(state.loadingTimer);
    var wrap = el('div', 'gs-loading');
    wrap.appendChild(el('span', 'gs-dots'));
    var status = el('p', 'gs-loading-text', LOADING_STEPS[0]);
    status.setAttribute('role', 'status');
    wrap.appendChild(status);
    els.body.appendChild(wrap);
    var step = 0;
    state.loadingTimer = setInterval(function () {
      if (!status.isConnected || step >= LOADING_STEPS.length - 1) { clearInterval(state.loadingTimer); return; }
      status.textContent = LOADING_STEPS[++step];
    }, 1600);
  }

  function post(body) {
    return fetch(ctx.api + '/search', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) { return { ok: r.ok, status: r.status, data: data }; });
    });
  }

  // Two requests at once: "ai" (personal picks and reasons, a few seconds; the
  // backend falls back to simple reasons itself after 8 s) and "instant"
  // (ranked picks, ~0.5 s). Shoppers see one list, never a list that swaps
  // itself out: the AI's, or the instant one only if the AI request fails
  // (rate limit, network). Showing instant picks first and replacing them
  // looked broken to shoppers (dev store, 2026-09-30).
  function search(refine) {
    skeleton();
    var seq = state.seq = (state.seq || 0) + 1;
    var a = state.answers;
    var body = {
      sid: ctx.sid, recipient: a.recipient, occasion: a.occasion, budget_band: a.budget_band,
      vibes: a.vibes.slice(), free_text: a.free_text.trim(), exclude_ids: state.shown.slice(-MAX_EXCLUDE),
      refine: refine === true, locale: ctx.locale
    };
    if (AGE_FOR[a.recipient]) body.age_band = AGE_FOR[a.recipient];
    var instant = null;

    var instantReady = post(Object.assign({}, body, { phase: 'instant' })).then(function (res) {
      if (res.ok && (res.data.picks || []).length) instant = res.data.picks;
    }).catch(function () { /* the AI request still decides */ });
    function fallback(message) {
      instantReady.then(function () {
        if (seq !== state.seq) return;
        clearInterval(state.loadingTimer);
        if (instant) renderResults(instant, false);
        else renderMessage(message);
      });
    }

    post(Object.assign({}, body, { phase: 'ai' })).then(function (res) {
      if (seq !== state.seq) return;
      if (res.ok) { clearInterval(state.loadingTimer); renderResults(res.data.picks || [], false); return; }
      fallback(res.status === 429 && res.data.detail ? res.data.detail : tr('Something went wrong. Please try again.'));
    }).catch(function () {
      if (seq !== state.seq) return;
      fallback(tr('Something went wrong. Please check your connection and try again.'));
    });
  }

  function actions(showMore, pending) {
    var row = el('div', 'gs-actions');
    if (showMore) {
      var more = el('button', 'gs-primary', tr('Show different ideas'));
      more.type = 'button';
      more.disabled = !!pending;
      more.addEventListener('click', function () { search(); });
      row.appendChild(more);
    }
    if (showMore && !pending) row.appendChild(refineControl());
    var back = el('button', 'gs-secondary', tr('Change answers'));
    back.type = 'button';
    back.addEventListener('click', function () { state.seq = (state.seq || 0) + 1; goTo(0); });
    row.appendChild(back);
    return row;
  }

  // ── "Not quite right?" ────────────────────────────────────────────────────
  function nextQuestion() {
    var vibes = state.answers.vibes;
    var qs = (ctx.config.intake.refine_questions || []);
    for (var i = 0; i < qs.length; i++) {
      var q = qs[i];
      var answered = q.options.some(function (o) { return vibes.indexOf(o.vibe) !== -1; });
      if (!answered && state.asked.indexOf(q.id) === -1) return q;
    }
    return null;
  }

  function refineControl() {
    var max = ctx.config.intake.max_refines || 2;
    var q = nextQuestion();
    if (state.refines >= max || !q) {
      var browse = el('a', 'gs-secondary', tr('Browse all products'));
      browse.href = root() + 'collections/all';
      return browse;
    }
    var b = el('button', 'gs-secondary', tr('Not quite right?'));
    b.type = 'button';
    b.addEventListener('click', function () { renderQuestion(q); });
    return b;
  }

  function renderQuestion(q) {
    clear(els.body);
    var p = el('p', 'gs-status', tr(q.question));
    p.setAttribute('role', 'status');
    els.body.appendChild(p);
    var row = el('div', 'gs-chips');
    q.options.forEach(function (o) {
      var b = el('button', 'gs-chip', tr(o.label));
      b.type = 'button';
      b.addEventListener('click', function () {
        var vibes = state.answers.vibes;
        if (vibes.indexOf(o.vibe) === -1) {
          vibes.push(o.vibe);
          var max = ctx.config.intake.max_vibes || 3;
          while (vibes.length > max) vibes.shift();
        }
        state.asked.push(q.id);
        state.refines += 1;
        track('refine');
        search(true);
      });
      row.appendChild(b);
    });
    els.body.appendChild(row);
    var back = el('button', 'gs-secondary', tr('Back to ideas'));
    back.type = 'button';
    back.style.marginTop = '12px';
    back.addEventListener('click', function () { renderResults(state.lastPicks || [], false, true); });
    els.body.appendChild(back);
    renderFoot();
    var first = els.body.querySelector('.gs-chip');
    if (first) first.focus();
  }

  // ── gift panel ────────────────────────────────────────────────────────────
  // Direct: the whole order is one gift, note in the visible "Gift note"
  // attribute. Self: each item is labelled "Gift for: <who>" and its group
  // (label + note) goes in the hidden _giftsense_gifts attribute. The order
  // webhook reads both (app/services/gift_orders.py).
  function labelFor(value) {
    var r = (ctx.config.intake.recipients || []).filter(function (o) { return o.value === value; })[0];
    return r ? r.label : '';
  }

  // An optional section folded to one line: its title, what's chosen (✓, a
  // wrap name, a date…) and a chevron. One open at a time, so the gift panel
  // fits a phone. `value` returns the summary; default: the selected chip.
  function collapse(section, value) {
    if (!section) return null;
    var label = section.querySelector('.gs-label');
    var acc = el('div', 'gs-acc');
    var head = el('button', 'gs-acc-head');
    head.type = 'button';
    head.setAttribute('aria-expanded', 'false');
    head.appendChild(el('span', 'gs-acc-title', label ? label.textContent : ''));
    var sum = el('span', 'gs-acc-sum');
    head.appendChild(sum);
    head.appendChild(el('span', 'gs-acc-chev'));
    if (label) label.classList.add('gs-acc-label');
    var body = el('div', 'gs-acc-body');
    body.hidden = true;
    body.appendChild(section);
    acc.appendChild(head);
    acc.appendChild(body);
    function refresh() {
      var picked = section.querySelector('.gs-chip[aria-pressed="true"]');
      sum.textContent = (value ? value() : picked && picked.textContent) || '';
    }
    ['click', 'input', 'change'].forEach(function (t) { body.addEventListener(t, function () { setTimeout(refresh, 0); }); });
    head.addEventListener('click', function () {
      var opening = body.hidden;
      els.body.querySelectorAll('.gs-acc').forEach(function (o) {
        o.querySelector('.gs-acc-body').hidden = true;
        o.querySelector('.gs-acc-head').setAttribute('aria-expanded', 'false');
      });
      body.hidden = !opening;
      head.setAttribute('aria-expanded', opening ? 'true' : 'false');
      if (opening && acc.scrollIntoView) acc.scrollIntoView({ block: 'nearest' });
    });
    refresh();
    return acc;
  }
  function done(v) { return v ? '\u2713' : ''; }

  function contextChips(title, options, key, onPick) {
    var wrap = el('fieldset', 'gs-group');
    wrap.appendChild(el('legend', 'gs-label', title));
    var row = el('div', 'gs-chips');
    options.forEach(function (o) {
      var b = el('button', 'gs-chip gs-small-chip', tr(o.label));
      b.type = 'button';
      b.setAttribute('aria-pressed', state.answers[key] === o.value ? 'true' : 'false');
      b.addEventListener('click', function () {
        state.answers[key] = o.value;
        row.querySelectorAll('.gs-chip').forEach(function (c) { c.setAttribute('aria-pressed', c === b ? 'true' : 'false'); });
        if (onPick) onPick(o.value);
      });
      row.appendChild(b);
    });
    wrap.appendChild(row);
    return wrap;
  }

  // Cart page: one gift note for the whole order (direct mode), nothing added.
  function renderCartNote() {
    var g = { note: '', tone: (ctx.config.notes || {}).tone || 'warm', name: '', left: null };
    clear(els.body);
    els.body.appendChild(el('p', 'gs-status', tr('Add a gift note to your order')));
    var intake = ctx.config.intake;
    els.body.appendChild(collapse(contextChips(tr('Who’s it for? (optional)'), intake.recipients, 'recipient')));
    els.body.appendChild(collapse(contextChips(tr('What’s the occasion? (optional)'), intake.occasions, 'occasion')));
    var section = noteSection(g, null);
    els.body.appendChild(section);
    var textarea = section.querySelector('textarea');
    var wrapHolder = el('div');
    els.body.appendChild(wrapHolder);
    var dateHolder = el('div');
    els.body.appendChild(dateHolder);
    var cartNow = null;
    fetch(root() + 'cart.js', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (cart) {
      cartNow = cart;
      var attrs = (cart && cart.attributes) || {};
      var existing = attrs['Gift note'];
      if (existing && !textarea.value) { textarea.value = g.note = existing; textarea.dispatchEvent(new Event('input')); }
      // Wrap for the whole order: only when there's something to wrap.
      var items = (cart && cart.items) || [];
      if (items.some(function (it) { return !(it.properties || {})._giftsense_wrap_for; })) {
        var wrapChoice = collapse(wrapSection(g, attrs['Gift wrap']));
        if (wrapChoice) wrapHolder.appendChild(wrapChoice);
        var cardChoice = collapse(cardSection(g, attrs['Greeting card']));
        if (cardChoice) wrapHolder.appendChild(cardChoice);
      }
      g.wrapBefore = attrs['Gift wrap'] || '';
      g.cardBefore = attrs['Greeting card'] || '';
      var dateChoice = collapse(dateSection(g, attrs['Arrive by']), function () { return g.arriveBy; });
      if (dateChoice) dateHolder.appendChild(dateChoice);
    }).catch(function () {
      var dateChoice = collapse(dateSection(g), function () { return g.arriveBy; });
      if (dateChoice) dateHolder.appendChild(dateChoice);
    });
    var row = el('div', 'gs-actions gs-sticky');
    var save = el('button', 'gs-primary', tr('Save gift note'));
    save.type = 'button';
    save.addEventListener('click', function () {
      save.disabled = true;
      var note = (g.note || '').trim();
      var attrs = { 'Gift note': note, 'Arrive by': g.arriveBy || '', _giftsense_sid: ctx.sid };
      var wrapNow = g.wrap ? g.wrap.name : '';
      var wrapChanged = cartNow && g.wrapBefore !== undefined && wrapNow !== g.wrapBefore;
      if (wrapChanged) attrs['Gift wrap'] = wrapNow;
      var cardNow = g.card ? g.card.name : '';
      var cardChanged = cartNow && g.cardBefore !== undefined && cardNow !== g.cardBefore;
      if (cardChanged) attrs['Greeting card'] = cardNow;
      orderAddon(wrapChanged ? cartNow : null, g.wrap, '_giftsense_wrap_for', 'Wrap for').then(function () {
        return orderAddon(cardChanged ? cartNow : null, g.card, '_giftsense_card_for', 'Card for');
      }).then(function () {
        return fetch(root() + 'cart/update.js', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ attributes: attrs })
        });
      }).then(function (r) {
        if (!r.ok) throw new Error('update');
        if (wrapChanged || cardChanged) refreshCart();
        clear(els.body);
        var done = el('p', 'gs-status', note ? tr('Gift note saved ✓') : tr('Gift note removed'));
        done.setAttribute('role', 'status');
        els.body.appendChild(done);
        var ok = el('button', 'gs-primary', tr('Done'));
        ok.type = 'button';
        ok.addEventListener('click', close);
        els.body.appendChild(ok);
        ok.focus();
      }).catch(function () { save.disabled = false; save.textContent = tr('Try again'); });
    });
    row.appendChild(save);
    var cancel = el('button', 'gs-secondary', tr('Cancel'));
    cancel.type = 'button';
    cancel.addEventListener('click', close);
    row.appendChild(cancel);
    els.body.appendChild(row);
    renderFoot();
  }

  // Whole-order wrap or card from the cart page / drawer: swap the order's
  // line (prop = "order") for the chosen style, or remove it.
  function orderAddon(cart, style, prop, label) {
    if (!cart) return Promise.resolve();
    var json = { 'Content-Type': 'application/json', Accept: 'application/json' };
    var chain = Promise.resolve();
    ((cart && cart.items) || []).forEach(function (it) {
      if ((it.properties || {})[prop] === 'order') {
        chain = chain.then(function () {
          return fetch(root() + 'cart/change.js', { method: 'POST', credentials: 'same-origin', headers: json,
            body: JSON.stringify({ id: it.key, quantity: 0 }) });
        });
      }
    });
    if (!style) return chain;
    return chain.then(function () {
      try { localStorage.setItem('giftsense:wrap', '1'); } catch (e) { /* private mode */ }
      if (window.GiftSenseArmCart) window.GiftSenseArmCart();
      track('wrap_added');
      return fetch(root() + 'cart/add.js', { method: 'POST', credentials: 'same-origin', headers: json,
        body: JSON.stringify({ items: [{ id: style.variant_id, quantity: 1,
          properties: (function () { var p = {}; p[prop] = 'order'; p[label] = 'Your order'; return p; })() }] }) });
    });
  }

  // After changing cart lines: the cart page reloads; drawers get the common
  // refresh events (themes differ in what they listen for).
  function refreshCart() {
    if (/\/cart\/?$/.test(location.pathname)) { location.reload(); return; }
    ['cart:refresh', 'cart:updated', 'cart:change'].forEach(function (n) { document.dispatchEvent(new CustomEvent(n)); });
  }

  // Note box + "Write it for me" (tones, first name, rewrites left); edits g.note.
  function noteSection(g, productId) {
    var notes = ctx.config.notes || { tone: 'warm', max_chars: 250, tones: [] };
    var noteWrap = el('div', 'gs-group');
    noteWrap.appendChild(el('span', 'gs-label', tr('Gift note (optional)')));
    var note = el('textarea', 'gs-note');
    note.rows = 3;
    note.maxLength = notes.max_chars;
    note.placeholder = tr('Write your message, or let us draft one.');
    var counter = el('span', 'gs-hint', '0/' + notes.max_chars);
    note.addEventListener('input', function () { g.note = note.value; counter.textContent = note.value.length + '/' + notes.max_chars; });
    noteWrap.appendChild(note);
    noteWrap.appendChild(counter);

    var toneRow = el('div', 'gs-chips gs-tones');
    (notes.tones || []).forEach(function (t) {
      var b = el('button', 'gs-chip gs-small-chip', tr(t.label));
      b.type = 'button';
      b.setAttribute('aria-pressed', t.value === g.tone ? 'true' : 'false');
      b.addEventListener('click', function () {
        g.tone = t.value;
        toneRow.querySelectorAll('.gs-chip').forEach(function (c) { c.setAttribute('aria-pressed', c === b ? 'true' : 'false'); });
      });
      toneRow.appendChild(b);
    });
    noteWrap.appendChild(toneRow);

    var name = el('input', 'gs-note');
    name.type = 'text';
    name.maxLength = 40;
    name.placeholder = tr('Their first name (optional)');
    name.addEventListener('input', function () { g.name = name.value.replace(/[^\p{L}\p{N} .'\-]/gu, ''); });
    noteWrap.appendChild(name);

    var draftBtn = el('button', 'gs-secondary gs-small', tr('✨ Write it for me'));
    draftBtn.type = 'button';
    var draftStatus = el('span', 'gs-hint');
    draftStatus.setAttribute('role', 'status');
    draftBtn.addEventListener('click', function () {
      draftBtn.disabled = true;
      draftStatus.textContent = tr('Writing…');
      fetch(ctx.api + '/note/draft', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ sid: ctx.sid, product_id: productId || null, recipient: state.answers.recipient,
          occasion: state.answers.occasion, tone: g.tone, name: g.name.trim(), locale: ctx.locale })
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, d: d }; });
      }).then(function (res) {
        if (!res.ok) {
          draftStatus.textContent = res.d.detail || tr('Couldn’t write a note right now.');
          return;
        }
        g.note = note.value = res.d.note;
        track('note_drafted', productId);
        counter.textContent = note.value.length + '/' + notes.max_chars;
        g.left = res.d.rewrites_left;
        draftStatus.textContent = g.left > 1 ? tr('Edit it any way you like. {n} rewrites left.', { n: g.left })
          : g.left === 1 ? tr('Edit it any way you like. 1 rewrite left.') : tr('Edit it any way you like.');
        draftBtn.textContent = tr('Rewrite');
        draftBtn.disabled = g.left === 0;
      }).catch(function () {
        draftStatus.textContent = tr('Couldn’t write a note right now.');
      }).then(function () { if (g.left !== 0) draftBtn.disabled = false; });
    });
    var draftRow = el('div', 'gs-cart');
    draftRow.appendChild(draftBtn);
    draftRow.appendChild(draftStatus);
    noteWrap.appendChild(draftRow);
    return noteWrap;
  }

  function renderGiftPanel(pick, variantId, opts) {
    opts = opts || {};
    if (!opts.standalone) track('panel_open', pick.product_id);
    var notes = ctx.config.notes || { tone: 'warm', max_chars: 250, tones: [] };
    var g = { mode: 'direct', label: labelFor(state.answers.recipient), note: '', tone: notes.tone, name: '', left: null };
    clear(els.body);
    els.body.appendChild(el('p', 'gs-status', tr('Add “{title}” as a gift', { title: pick.title })));

    // Same stepped pattern as the finder's questions: a progress bar, one
    // screen at a time, Back / Next; picking a context chip moves on. Screens
    // are built once and only shown/hidden, so answers survive going back.
    var screens = [];
    var current = 0;
    function autoNext() {
      var at = current, seq = state.seq = (state.seq || 0) + 1;
      setTimeout(function () { if (state.seq === seq && current === at) show(at + 1); }, 180);
    }

    var labelWrap = el('label', 'gs-group');
    var label = el('input', 'gs-note');
    // Product page: always ask (pre-selected if answered in the finder), so the
    // flow is the same 4 steps every time. From the finder's ideas it's known.
    if (opts.askContext) {
      var intake = ctx.config.intake;
      screens.push(contextChips(tr('Who’s it for? (optional)'), intake.recipients, 'recipient', function (v) {
        if (!label.value || label.value === g.label) { g.label = label.value = labelFor(v); }
        autoNext();
      }));
      screens.push(contextChips(tr('What’s the occasion? (optional)'), intake.occasions, 'occasion', autoNext));
    }

    var where = el('div');
    var modes = el('fieldset', 'gs-group');
    modes.appendChild(el('legend', 'gs-label', tr('Where is this going?')));
    var modeRow = el('div', 'gs-chips');
    [['direct', tr('Ship it straight to them')], ['self', tr('I’ll give it to them')]].forEach(function (m) {
      var b = el('button', 'gs-chip', m[1]);
      b.type = 'button';
      b.setAttribute('data-value', m[0]);
      b.addEventListener('click', function () {
        g.mode = m[0];
        modeRow.querySelectorAll('.gs-chip').forEach(function (c) {
          c.setAttribute('aria-pressed', c.getAttribute('data-value') === g.mode ? 'true' : 'false');
        });
        labelWrap.hidden = g.mode !== 'self';
        if (g.mode === 'self') label.focus();
      });
      b.setAttribute('aria-pressed', m[0] === g.mode ? 'true' : 'false');
      modeRow.appendChild(b);
    });
    modes.appendChild(modeRow);
    where.appendChild(modes);

    labelWrap.appendChild(el('span', 'gs-label', tr('Who’s it for?')));
    label.type = 'text';
    label.maxLength = 40;
    label.value = g.label;
    label.placeholder = tr('e.g. Mom');
    label.addEventListener('input', function () { g.label = label.value; });
    labelWrap.appendChild(label);
    labelWrap.hidden = true;
    where.appendChild(labelWrap);
    screens.push(where);

    // The note is the heart of a gift; the extras fold to one line each below it.
    var extras = el('div');
    extras.appendChild(noteSection(g, pick.product_id));
    [collapse(messageSection(g), function () { return done(g.message); }),
     pick.wrap === false ? null : collapse(wrapSection(g)),
     collapse(cardSection(g)),
     collapse(dateSection(g), function () { return g.arriveBy; })
    ].forEach(function (c) { if (c) extras.appendChild(c); });
    screens.push(extras);

    var progress = el('div', 'gs-progress');
    var progressText = el('span', 'gs-progress-text');
    var bar = el('div', 'gs-progress-bar');
    var fill = el('span', 'gs-progress-fill');
    bar.appendChild(fill);
    progress.appendChild(progressText);
    progress.appendChild(bar);
    els.body.appendChild(progress);
    screens.forEach(function (s) { s.hidden = true; els.body.appendChild(s); });

    var nav = el('div', 'gs-nav');
    var back = el('button', 'gs-secondary');
    back.type = 'button';
    back.addEventListener('click', function () {
      if (current > 0) show(current - 1);
      else if (opts.standalone) close();
      else renderResults(state.lastPicks || [], false, true);
    });
    var next = el('button', 'gs-primary');
    next.type = 'button';
    next.addEventListener('click', function () {
      if (current < screens.length - 1) show(current + 1);
      else addGift(pick, variantId, g, next, opts.convert);
    });
    nav.appendChild(back);
    nav.appendChild(next);
    els.body.appendChild(nav);

    function show(n) {
      state.seq = (state.seq || 0) + 1;   // cancels a pending auto-advance
      current = n;
      screens.forEach(function (s, i) { s.hidden = i !== n; });
      progressText.textContent = tr('Step {n} of {total}', { n: n + 1, total: screens.length });
      fill.style.width = (100 * (n + 1) / screens.length) + '%';
      back.textContent = n > 0 ? tr('Back') : opts.standalone ? tr('Cancel') : tr('Back to ideas');
      var last = n === screens.length - 1;
      next.textContent = last ? tr('Add gift to cart') : tr('Next');
      next.disabled = false;
      els.body.scrollTop = 0;
      var first = screens[n].querySelector('.gs-chip[aria-pressed="true"]') || screens[n].querySelector('.gs-chip, textarea, button');
      if (first) first.focus();
    }

    renderFoot();
    show(0);
  }

  // Voice (Growth+) / video (Pro) message: recorded in the browser, uploaded
  // straight to storage with a presigned URL (/media/upload-url), confirmed
  // (/media/confirm), and its token rides on the gift group like the note.
  // The consent line shows before anything is recorded.
  var RECORD_TYPES = {
    voice: ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm', 'audio/ogg'],
    video: ['video/webm;codecs=vp9,opus', 'video/webm', 'video/mp4']
  };

  function messageSection(g) {
    var m = ctx.config.media || {};
    var kinds = (m.kinds || []).filter(function (k) {
      return window.MediaRecorder && navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
        RECORD_TYPES[k].some(function (t) { return MediaRecorder.isTypeSupported(t); });
    });
    if (!kinds.length) return null;
    g.message = null;
    var box = el('fieldset', 'gs-group');
    box.appendChild(el('legend', 'gs-label', kinds.length > 1 ? tr('Add a voice or video message (optional)') : tr('Add a voice message (optional)')));
    var area = el('div', 'gs-media');
    var status = el('span', 'gs-hint gs-hint-left');
    status.setAttribute('role', 'status');
    box.appendChild(area);
    box.appendChild(status);

    var recorder = null, stream = null, chunks = [], timer = null, started = 0, kind = null, blob = null, mime = '';

    function stopStream() {
      if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
      clearInterval(timer);
    }
    function button(text, cls, onClick) {
      var b = el('button', cls || 'gs-secondary gs-small', text);
      b.type = 'button';
      b.addEventListener('click', onClick);
      return b;
    }
    function secs(n) { return Math.floor(n / 60) + ':' + ('0' + Math.floor(n % 60)).slice(-2); }

    function idle(msg) {
      stopStream();
      clear(area);
      g.message = null;
      blob = null;
      area.appendChild(el('p', 'gs-hint gs-hint-left', tr('Anyone with the QR code or link can watch or listen to this.')));
      var row = el('div', 'gs-chips');
      kinds.forEach(function (k) {
        row.appendChild(button(k === 'voice' ? tr('🎙 Record voice') : tr('🎥 Record video'), null, function () { record(k); }));
      });
      area.appendChild(row);
      status.textContent = msg || '';
    }

    function record(k) {
      kind = k;
      mime = RECORD_TYPES[k].filter(function (t) { return MediaRecorder.isTypeSupported(t); })[0];
      var max = (m.max_secs || {})[k] || (k === 'voice' ? 120 : 60);
      var constraints = k === 'voice' ? { audio: true }
        : { audio: true, video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' } };
      status.textContent = k === 'video' ? tr('Allow camera and microphone access to record.') : tr('Allow microphone access to record.');
      navigator.mediaDevices.getUserMedia(constraints).then(function (s) {
        stream = s;
        chunks = [];
        recorder = new MediaRecorder(s, { mimeType: mime, audioBitsPerSecond: 64000, videoBitsPerSecond: 2500000 });
        recorder.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
        recorder.onstop = function () { stopStream(); review(); };
        clear(area);
        if (k === 'video') {
          var live = el('video', 'gs-media-preview');
          live.muted = true; live.playsInline = true; live.autoplay = true;
          live.srcObject = s;
          area.appendChild(live);
        }
        var clock = el('span', 'gs-hint gs-hint-left', '● 0:00 / ' + secs(max));
        area.appendChild(clock);
        area.appendChild(button(tr('Stop'), 'gs-primary gs-small', function () { recorder.stop(); }));
        status.textContent = tr('Recording…');
        started = Date.now();
        timer = setInterval(function () {
          var t = (Date.now() - started) / 1000;
          clock.textContent = '● ' + secs(t) + ' / ' + secs(max);
          if (t >= max && recorder.state === 'recording') recorder.stop();
        }, 250);
        recorder.start(1000);
      }).catch(function () {
        idle(k === 'video' ? tr('We couldn’t use your camera. Check the browser’s permission and try again.')
          : tr('We couldn’t use your microphone. Check the browser’s permission and try again.'));
      });
    }

    function review() {
      var duration = Math.max(1, Math.round((Date.now() - started) / 1000));
      blob = new Blob(chunks, { type: mime.split(';')[0] });
      clear(area);
      var player = el(kind === 'video' ? 'video' : 'audio', 'gs-media-preview');
      player.controls = true; player.playsInline = true;
      player.src = URL.createObjectURL(blob);
      area.appendChild(player);
      var row = el('div', 'gs-chips');
      row.appendChild(button(tr('Use this'), 'gs-primary gs-small', function () { upload(duration, row); }));
      row.appendChild(button(tr('Record again'), null, function () { idle(); }));
      area.appendChild(row);
      status.textContent = tr('Listen back, then choose Use this.');
    }

    function upload(duration, row) {
      row.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
      status.textContent = tr('Saving your message…');
      var json = { 'Content-Type': 'application/json', Accept: 'application/json' };
      var token;
      fetch(ctx.api + '/media/upload-url', {
        method: 'POST', credentials: 'same-origin', headers: json,
        body: JSON.stringify({ sid: ctx.sid, kind: kind, mime: blob.type, size: blob.size, duration_s: duration })
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok) throw new Error(d.detail || 'save');
          token = d.token;
          return fetch(d.upload_url, { method: 'PUT', headers: d.headers, body: blob });
        });
      }).then(function (r) {
        if (!r.ok) throw new Error('save');
        return fetch(ctx.api + '/media/confirm', {
          method: 'POST', credentials: 'same-origin', headers: json, body: JSON.stringify({ sid: ctx.sid, token: token })
        });
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok) throw new Error(d.detail || 'save');
          g.message = d.token;
          track('message_added');
          clear(area);
          area.appendChild(el('span', 'gs-hint gs-hint-left', kind === 'video' ? tr('Video message added ✓') : tr('Voice message added ✓')));
          area.appendChild(button(tr('Remove'), null, function () { idle(); }));
          status.textContent = kind === 'video' ? tr('The recipient scans the QR code on the gift card to watch.')
            : tr('The recipient scans the QR code on the gift card to listen.');
        });
      }).catch(function (e) {
        row.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
        status.textContent = e && e.message && e.message !== 'save' ? e.message : tr('We couldn’t save your message. Please try again.');
      });
    }

    idle();
    return box;
  }

  // Gift wrap: merchant styles from /config. "No wrap" is the default (never
  // pre-select a paid add-on) and every style shows its price.
  // Arrive-by (Growth+): a date picker limited to what the store can make
  // (/config delivery.earliest..latest, store time). One date per order.
  function dateSection(g, existing) {
    var d = ctx.config.delivery;
    if (!d || !d.earliest) return null;
    var box = el('label', 'gs-group');
    box.appendChild(el('span', 'gs-label', tr('When should it arrive? (optional)')));
    var input = el('input', 'gs-note gs-date');
    input.type = 'date';
    input.min = d.earliest;
    input.max = d.latest;
    if (existing && existing >= d.earliest && existing <= d.latest) input.value = g.arriveBy = existing;
    var hint = el('span', 'gs-hint gs-hint-left', tr('Estimated delivery. Applies to the whole order.'));
    input.addEventListener('change', function () {
      var v = input.value;
      if (v && (v < d.earliest || v > d.latest)) {
        hint.textContent = tr('Please pick a date between {a} and {b}.', { a: d.earliest, b: d.latest });
        g.arriveBy = '';
        return;
      }
      g.arriveBy = v;
      hint.textContent = tr('Estimated delivery. Applies to the whole order.');
    });
    box.appendChild(input);
    box.appendChild(hint);
    return box;
  }

  // Styles are a wrap, bag or box (the merchant's choice of type); `current`
  // preselects what the cart already has (cart page / drawer).
  var KIND_MARK = { wrap: '🎀 ', bag: '🛍 ', box: '📦 ', card: '💌 ' };

  function wrapSection(g, current) { return addonSection(g, current, false); }
  function cardSection(g, current) { return addonSection(g, current, true); }

  // Packaging (wrap/bag/box, g.wrap) or greeting cards (g.card): one choice
  // each, "No wrap" / "No card" by default.
  function addonSection(g, current, cards) {
    var key = cards ? 'card' : 'wrap';
    var styles = (ctx.config.wrap || []).filter(function (w) { return (w.kind === 'card') === cards; });
    if (!styles.length) return null;
    g[key] = styles.filter(function (w) { return current && w.name === current; })[0] || null;
    var mixed = !cards && styles.some(function (w) { return (w.kind || 'wrap') !== 'wrap'; });
    var box = el('fieldset', 'gs-group');
    box.appendChild(el('legend', 'gs-label', cards ? tr('Greeting card (optional)')
      : mixed ? tr('Gift packaging (optional)') : tr('Gift wrap (optional)')));
    var row = el('div', 'gs-chips');
    [null].concat(styles).forEach(function (w) {
      var mark = w && !w.image ? (KIND_MARK[w.kind] || '') : '';
      var b = el('button', 'gs-chip gs-small-chip', w ? mark + w.name + ' · ' + (w.price ? '+' + money(w.price) : tr('Free')) : cards ? tr('No card') : tr('No wrap'));
      b.type = 'button';
      if (w && w.image) {
        // Shopify CDN: ask for a small square so the panel stays light.
        var img = el('img', 'gs-wrap-img');
        img.src = w.image + (w.image.indexOf('?') < 0 ? '?' : '&') + 'width=64&height=64&crop=center';
        img.alt = ''; img.width = 20; img.height = 20; img.loading = 'lazy';
        b.insertBefore(img, b.firstChild);
        b.classList.add('gs-wrap-chip');
      }
      b.setAttribute('aria-pressed', w === g[key] ? 'true' : 'false');
      b.addEventListener('click', function () {
        g[key] = w;
        row.querySelectorAll('.gs-chip').forEach(function (c) { c.setAttribute('aria-pressed', c === b ? 'true' : 'false'); });
      });
      row.appendChild(b);
    });
    box.appendChild(row);
    return box;
  }

  // convert: the item is already in the cart as a plain line; that line goes
  // down by one (removed if it was one) and the gift line replaces it.
  function addGift(pick, variantId, g, button, convert) {
    var json = { 'Content-Type': 'application/json', Accept: 'application/json' };
    var label = (g.label || '').trim().slice(0, 40) || 'Gift';
    var note = (g.note || '').trim();
    button.disabled = true;
    button.textContent = tr('Adding…');
    fetch(root() + 'cart.js', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (cart) {
      var attrs = (cart && cart.attributes) || {};
      var groups = [];
      try { groups = JSON.parse(attrs._giftsense_gifts || '[]'); } catch (e) { groups = []; }
      if (!Array.isArray(groups)) groups = [];
      // Merchants see every order field in Shopify admin, so keep them few: the
      // session id once on the order (attribution) and a gift marker per item.
      // Delivery mode is implied: gift groups (_giftsense_gifts) mean "to me".
      var props = {};
      var update = { _giftsense_sid: ctx.sid };
      var items = [{ id: variantId, quantity: 1, properties: props }];
      // One wrap line per gift group (direct mode: one for the whole order),
      // linked by _giftsense_wrap_for. A group that already has wrap keeps it.
      var addWrap = function (groupId, wrapLabel, already) {
        if (!g.wrap || already) return;
        items.push({ id: g.wrap.variant_id, quantity: 1,
          properties: { _giftsense_wrap_for: groupId, 'Wrap for': wrapLabel } });
      };
      // A greeting card works the same way, on its own line (_giftsense_card_for).
      var addCard = function (groupId, cardLabel, already) {
        if (!g.card || already) return;
        items.push({ id: g.card.variant_id, quantity: 1,
          properties: { _giftsense_card_for: groupId, 'Card for': cardLabel } });
      };
      if (g.mode === 'self') {
        var group = groups.filter(function (x) { return (x.label || '').toLowerCase() === label.toLowerCase(); })[0];
        if (!group) {
          group = { id: 'g' + Math.random().toString(36).slice(2, 8), label: label };
          groups.push(group);
        }
        if (note) group.note = note;
        if (g.message) group.message = g.message;
        addWrap(group.id, label, group.wrap);
        if (g.wrap && !group.wrap) group.wrap = g.wrap.name;
        addCard(group.id, label, group.card);
        if (g.card && !group.card) group.card = g.card.name;
        props['Gift for'] = label;
        props._giftsense_gift = group.id;
        update._giftsense_gifts = JSON.stringify(groups);
      } else {
        props._giftsense_gift = 'order';
        if (note) update['Gift note'] = note;
        if (g.message) update._giftsense_message = g.message;
        addWrap('order', 'Your gift', attrs['Gift wrap']);
        if (g.wrap && !attrs['Gift wrap']) update['Gift wrap'] = g.wrap.name;
        addCard('order', 'Your gift', attrs['Greeting card']);
        if (g.card && !attrs['Greeting card']) update['Greeting card'] = g.card.name;
      }
      if (g.arriveBy) update['Arrive by'] = g.arriveBy;
      if (items.length > 1) {
        track('wrap_added', pick.product_id);
        // Arms the wrap cart guard (giftsense-cart.js) now and on later pages.
        try { localStorage.setItem('giftsense:wrap', '1'); } catch (e) { /* private mode */ }
        if (window.GiftSenseArmCart) window.GiftSenseArmCart();
      }
      var plain = convert ? plainLine(cart, pick.product_id) : null;
      var swap = plain ? fetch(root() + 'cart/change.js', {
        method: 'POST', credentials: 'same-origin', headers: json,
        body: JSON.stringify({ id: plain.key, quantity: plain.quantity - 1 })
      }) : Promise.resolve();
      return swap.then(function () {
        return fetch(root() + 'cart/add.js', {
          method: 'POST', credentials: 'same-origin', headers: json,
          body: JSON.stringify({ items: items })
        });
      }).then(function (r) {
        if (!r.ok) throw new Error('add');
        return fetch(root() + 'cart/update.js', {
          method: 'POST', credentials: 'same-origin', headers: json, body: JSON.stringify({ attributes: update })
        });
      });
    }).then(function () {
      clear(els.body);
      track('panel_submit', pick.product_id);
      if (pick.product_id) state.added[pick.product_id] = 'gift';
      save();
      var done = el('p', 'gs-status', tr('Added to your cart as a gift ✓'));
      done.setAttribute('role', 'status');
      els.body.appendChild(done);
      var row = el('div', 'gs-actions');
      var view = el('a', 'gs-primary', tr('View cart'));
      view.href = root() + 'cart';
      newTab(view);
      row.appendChild(view);
      var more = el('button', 'gs-secondary', tr('Keep looking'));
      more.type = 'button';
      more.addEventListener('click', function () { renderResults(state.lastPicks || [], false, true); });
      row.appendChild(more);
      els.body.appendChild(row);
      renderFoot();
      view.focus();
      document.dispatchEvent(new CustomEvent('giftsense:added-to-cart', { detail: { variantId: variantId, gift: true } }));
    }).catch(function () {
      button.disabled = false;
      button.textContent = tr('Try again');
    });
  }

  function renderMessage(text) {
    clear(els.body);
    var p = el('p', 'gs-status', text);
    p.setAttribute('role', 'alert');
    els.body.appendChild(p);
    els.body.appendChild(actions(false));
    renderFoot();
  }

  function renderResults(picks, pending, restoring) {
    var replacing = !!els.body.querySelector('.gs-card:not(.gs-card--skeleton)');
    var oldRow = els.body.querySelector('.gs-results');
    var keepScroll = replacing && oldRow ? oldRow.scrollLeft : 0;
    clear(els.body);
    if (!picks.length) {
      renderMessage(state.shown.length
        ? tr('That\u2019s all we found for these answers. Try a different budget or occasion.')
        : tr('We couldn\u2019t find a gift for that budget. Try another budget or occasion.'));
      return;
    }
    var heading = el('p', 'gs-status' + (pending ? ' gs-status--pending' : ''),
      pending ? tr('Here are some ideas \u00B7 personalizing\u2026') : tr('Here are some ideas'));
    heading.setAttribute('role', 'status');
    els.body.appendChild(heading);
    // Picks sit side by side in a swipeable row (scroll-snap) so the dialog
    // stays short; arrows page through it where there's a mouse.
    var wrap = el('div', 'gs-results-wrap');
    var row = el('div', 'gs-results');
    row.setAttribute('role', 'list');
    wrap.appendChild(row);
    [['prev', -1, '\u2039', tr('Previous ideas')], ['next', 1, '\u203A', tr('More ideas')]].forEach(function (a) {
      var b = el('button', 'gs-arrow gs-arrow--' + a[0], a[2]);
      b.type = 'button';
      b.setAttribute('aria-label', a[3]);
      b.addEventListener('click', function () { row.scrollBy({ left: a[1] * row.clientWidth * 0.8, behavior: 'smooth' }); });
      wrap.appendChild(b);
    });
    function arrows() {
      var end = row.scrollWidth - row.clientWidth - 2;
      wrap.querySelector('.gs-arrow--prev').hidden = row.scrollLeft <= 2;
      wrap.querySelector('.gs-arrow--next').hidden = row.scrollLeft >= end;
    }
    row.addEventListener('scroll', arrows, { passive: true });
    picks.forEach(function (p) {
      if (!pending && !restoring) state.shown.push(p.product_id);   // only final picks are excluded next time
      var href = safeHref(p.url);
      var card = el('div', 'gs-card');
      card.setAttribute('role', 'listitem');
      var link = el(href ? 'a' : 'div', 'gs-card-link');
      if (href) newTab(link, p.title).href = href;
      var src = sized(p.image_url, 480);
      if (src) {
        var img = el('img', 'gs-img');
        img.src = src;
        img.alt = '';
        img.loading = 'lazy';
        link.appendChild(img);
      } else {
        link.appendChild(el('div', 'gs-img'));
      }
      card.appendChild(link);
      var text = el('div', 'gs-text');
      var name = el(href ? 'a' : 'span', 'gs-name', p.title);
      if (href) newTab(name).href = href;
      (function (id) {
        [link, name].forEach(function (a) { if (a.href) a.addEventListener('click', function () { track('pick_click', id); flush(); }); });
      })(p.product_id);
      text.appendChild(name);
      text.appendChild(el('span', 'gs-price', p.price_min === p.price_max
        ? money(p.price_min) : money(p.price_min) + ' \u2013 ' + money(p.price_max)));
      text.appendChild(el('span', 'gs-reason' + (pending ? ' gs-reason--pending' : ''), p.reason));
      if (!pending) {
        var slot = el('div', 'gs-cart');
        text.appendChild(slot);
        cartButton(p, slot);
      }
      card.appendChild(text);
      row.appendChild(card);
    });
    els.body.appendChild(wrap);
    if (keepScroll) row.scrollLeft = keepScroll;
    setTimeout(arrows, 0);
    if (!pending) { state.lastPicks = picks; state.view = 'results'; save(); }
    els.body.appendChild(actions(true, pending));
    renderFoot();
    // Move focus only on the first render, never when the AI picks swap in.
    // Focus goes to the heading (announced, no ring around a product image).
    if (!replacing) {
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
    }
  }

  // "Add to registry" on product pages (Pro): adds the variant selected in the
  // product form to the logged-in shopper's registry; a second click opens it.
  function addToRegistry(b, api) {
    if (b.getAttribute('data-added')) { location.href = api + '/registry'; return; }
    var field = document.querySelector('form[action*="/cart/add"] [name="id"]');
    var root = b.closest('[data-product-id]');
    var vid = String((field && field.value) || root.getAttribute('data-variant-id'));
    var ids = [], titles = [];
    try { ids = JSON.parse(b.getAttribute('data-variant-ids')); titles = JSON.parse(b.getAttribute('data-variant-titles')); } catch (e) { /* keep empty */ }
    var i = ids.map(String).indexOf(vid);
    var label = b.textContent;
    b.disabled = true;
    b.textContent = tr('Adding…');
    fetch(api + '/registry/items', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ product_id: root.getAttribute('data-product-id'), variant_id: vid,
        variant_title: i >= 0 ? titles[i] : '' })
    }).then(function (r) {
      if (r.status === 401) { location.href = b.getAttribute('data-login-url'); return; }
      if (r.ok) b.setAttribute('data-added', '1');
      b.textContent = r.ok ? tr('Added ✓ View your registry') : tr('Couldn’t add. Try again');
    }).catch(function () { b.textContent = label; }).then(function () { b.disabled = false; });
  }

  window.GiftSenseUI = { open: open, close: close, addToRegistry: addToRegistry };
})();
