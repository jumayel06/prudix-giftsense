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
    shown: [], refines: 0, asked: [] };
  var ctx = null;
  var els = {};

  // ── tiny DOM helpers ──────────────────────────────────────────────────────
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

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

  function handleOf(url) {
    var m = typeof url === 'string' && url.match(/^\/products\/([\w-]+)$/);
    return m ? m[1] : null;
  }

  // ── add to cart ───────────────────────────────────────────────────────────
  // Variants come from the theme's own /products/<handle>.js. One variant →
  // add it here; several (sizes, colors) → send the shopper to the product
  // page. Hidden `_giftsense_*` line properties and cart attribute let the
  // order be attributed to the gift finder (never customer data).
  function cartButton(pick, slot) {
    var handle = handleOf(pick.url);
    if (!handle) return;
    fetch(root() + 'products/' + handle + '.js', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (product) {
        if (!product || !product.variants || !slot.isConnected) return;
        var available = product.variants.filter(function (v) { return v.available; });
        if (!available.length) {
          var sold = el('button', 'gs-secondary gs-small', 'Sold out');
          sold.type = 'button';
          sold.disabled = true;
          slot.appendChild(sold);
          return;
        }
        if (product.variants.length > 1) {
          var choose = el('a', 'gs-secondary gs-small', 'Choose options');
          choose.href = safeHref(pick.url);
          slot.appendChild(choose);
          return;
        }
        var gift = el('button', 'gs-primary gs-small', 'Add as a gift');
        gift.type = 'button';
        gift.addEventListener('click', function () { renderGiftPanel(pick, available[0].id); });
        slot.appendChild(gift);
        var add = el('button', 'gs-secondary gs-small', 'Add to cart');
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
    button.textContent = 'Adding\u2026';
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
      button.textContent = 'Added \u2713';
      track('pick_atc', productId);
      var view = el('a', 'gs-link', 'View cart');
      view.href = root() + 'cart';
      slot.appendChild(view);
      document.dispatchEvent(new CustomEvent('giftsense:added-to-cart', { detail: { variantId: variantId } }));
    }).catch(function () {
      button.disabled = false;
      button.textContent = 'Try again';
    });
  }

  function sized(url, width) {
    if (typeof url !== 'string' || url.indexOf('https://') !== 0) return null;
    return url + (url.indexOf('?') === -1 ? '?' : '&') + 'width=' + width;
  }

  // ── dialog shell ──────────────────────────────────────────────────────────
  function build() {
    els.overlay = el('div', 'gs-overlay');
    els.overlay.addEventListener('click', function (e) { if (e.target === els.overlay) close(); });
    els.dialog = el('div', 'gs-dialog');
    els.dialog.setAttribute('role', 'dialog');
    els.dialog.setAttribute('aria-modal', 'true');
    els.dialog.setAttribute('aria-labelledby', 'gs-title');

    var head = el('div', 'gs-head');
    var title = el('h2', 'gs-title', 'Find the perfect gift');
    title.id = 'gs-title';
    var x = el('button', 'gs-close', '×');
    x.type = 'button';
    x.setAttribute('aria-label', 'Close');
    x.addEventListener('click', close);
    head.appendChild(title);
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
    document.getElementById('gs-title').textContent =
      ctx.entry && ctx.entry.type !== 'finder' ? 'Gift options' : 'Find the perfect gift';
    var entry = ctx.entry;
    if (entry && entry.type === 'product' && entry.product && entry.product.variant_id) {
      renderGiftPanel({ product_id: entry.product.product_id, title: entry.product.title },
                      Number(entry.product.variant_id), { askContext: true, standalone: true });
    } else if (entry && entry.type === 'cart') {
      renderCartNote();
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
    if (ctx.config.show_badge) els.foot.appendChild(el('span', 'gs-badge', 'Gift ideas by GiftSense'));
  }

  // ── step 1: questions ─────────────────────────────────────────────────────
  function chipGroup(label, options, key, multi, max) {
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
      var b = el('button', 'gs-chip', o.label);
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
    els.go.disabled = !ready();
    els.hint.hidden = ready();
  }

  function renderIntake() {
    var intake = ctx.config.intake;
    clear(els.body);
    els.go = null;
    els.body.appendChild(chipGroup('Who is it for?', intake.recipients, 'recipient'));
    els.body.appendChild(chipGroup("What's the occasion?", intake.occasions, 'occasion'));
    els.body.appendChild(chipGroup("What's your budget?", intake.budgets, 'budget_band'));
    els.body.appendChild(chipGroup('They are\u2026 (pick up to ' + intake.max_vibes + ')', intake.vibes, 'vibes', true, intake.max_vibes));

    var noteWrap = el('label', 'gs-group');
    noteWrap.appendChild(el('span', 'gs-label', 'Anything else? (optional)'));
    var note = el('textarea', 'gs-note');
    note.rows = 2;
    note.maxLength = intake.max_free_text || 200;
    note.placeholder = 'e.g. loves hiking and strong coffee';
    note.value = state.answers.free_text;
    note.addEventListener('input', function () { state.answers.free_text = note.value; });
    noteWrap.appendChild(note);
    els.body.appendChild(noteWrap);

    els.go = el('button', 'gs-primary', 'Show gift ideas');
    els.go.type = 'button';
    els.go.addEventListener('click', function () {
      state.shown = []; state.refines = 0; state.asked = [];
      track('intake_complete');
      search();
    });
    els.body.appendChild(els.go);
    els.hint = el('p', 'gs-hint', 'Pick who it\u2019s for, the occasion and a budget.');
    els.body.appendChild(els.hint);
    updateGo();
    renderFoot();
    els.body.scrollTop = 0;
    var first = els.body.querySelector('.gs-chip');
    if (first) first.focus();
  }

  // ── step 2: results ───────────────────────────────────────────────────────
  // Loading state while the AI picks (a few seconds): a short line of text
  // that moves on, with a small animated indicator. No empty placeholder boxes.
  var LOADING_STEPS = ['Looking through the store\u2026', 'Picking the best matches\u2026', 'Writing why each one fits\u2026'];
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
      refine: refine === true
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
      fallback(res.status === 429 && res.data.detail ? res.data.detail : 'Something went wrong. Please try again.');
    }).catch(function () {
      if (seq !== state.seq) return;
      fallback('Something went wrong. Please check your connection and try again.');
    });
  }

  function actions(showMore, pending) {
    var row = el('div', 'gs-actions');
    if (showMore) {
      var more = el('button', 'gs-primary', 'Show different ideas');
      more.type = 'button';
      more.disabled = !!pending;
      more.addEventListener('click', function () { search(); });
      row.appendChild(more);
    }
    if (showMore && !pending) row.appendChild(refineControl());
    var back = el('button', 'gs-secondary', 'Change answers');
    back.type = 'button';
    back.addEventListener('click', function () { state.seq = (state.seq || 0) + 1; renderIntake(); });
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
      var browse = el('a', 'gs-secondary', 'Browse all products');
      browse.href = root() + 'collections/all';
      return browse;
    }
    var b = el('button', 'gs-secondary', 'Not quite right?');
    b.type = 'button';
    b.addEventListener('click', function () { renderQuestion(q); });
    return b;
  }

  function renderQuestion(q) {
    clear(els.body);
    var p = el('p', 'gs-status', q.question);
    p.setAttribute('role', 'status');
    els.body.appendChild(p);
    var row = el('div', 'gs-chips');
    q.options.forEach(function (o) {
      var b = el('button', 'gs-chip', o.label);
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
    var back = el('button', 'gs-secondary', 'Back to ideas');
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

  function contextChips(title, options, key, onPick) {
    var wrap = el('fieldset', 'gs-group');
    wrap.appendChild(el('legend', 'gs-label', title));
    var row = el('div', 'gs-chips');
    options.forEach(function (o) {
      var b = el('button', 'gs-chip gs-small-chip', o.label);
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
    els.body.appendChild(el('p', 'gs-status', 'Add a gift note to your order'));
    var intake = ctx.config.intake;
    els.body.appendChild(contextChips('Who’s it for? (optional)', intake.recipients, 'recipient'));
    els.body.appendChild(contextChips('What’s the occasion? (optional)', intake.occasions, 'occasion'));
    var section = noteSection(g, null);
    els.body.appendChild(section);
    var textarea = section.querySelector('textarea');
    var dateHolder = el('div');
    els.body.appendChild(dateHolder);
    fetch(root() + 'cart.js', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (cart) {
      var attrs = (cart && cart.attributes) || {};
      var existing = attrs['Gift note'];
      if (existing && !textarea.value) { textarea.value = g.note = existing; textarea.dispatchEvent(new Event('input')); }
      var dateChoice = dateSection(g, attrs['Arrive by']);
      if (dateChoice) dateHolder.appendChild(dateChoice);
    }).catch(function () {
      var dateChoice = dateSection(g);
      if (dateChoice) dateHolder.appendChild(dateChoice);
    });
    var row = el('div', 'gs-actions');
    var save = el('button', 'gs-primary', 'Save gift note');
    save.type = 'button';
    save.addEventListener('click', function () {
      save.disabled = true;
      var note = (g.note || '').trim();
      fetch(root() + 'cart/update.js', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ attributes: { 'Gift note': note, 'Arrive by': g.arriveBy || '', _giftsense_sid: ctx.sid } })
      }).then(function (r) {
        if (!r.ok) throw new Error('update');
        clear(els.body);
        var done = el('p', 'gs-status', note ? 'Gift note saved ✓' : 'Gift note removed');
        done.setAttribute('role', 'status');
        els.body.appendChild(done);
        var ok = el('button', 'gs-primary', 'Done');
        ok.type = 'button';
        ok.addEventListener('click', close);
        els.body.appendChild(ok);
        ok.focus();
      }).catch(function () { save.disabled = false; save.textContent = 'Try again'; });
    });
    row.appendChild(save);
    var cancel = el('button', 'gs-secondary', 'Cancel');
    cancel.type = 'button';
    cancel.addEventListener('click', close);
    row.appendChild(cancel);
    els.body.appendChild(row);
    renderFoot();
  }

  // Note box + "Write it for me" (tones, first name, rewrites left); edits g.note.
  function noteSection(g, productId) {
    var notes = ctx.config.notes || { tone: 'warm', max_chars: 250, tones: [] };
    var noteWrap = el('div', 'gs-group');
    noteWrap.appendChild(el('span', 'gs-label', 'Gift note (optional)'));
    var note = el('textarea', 'gs-note');
    note.rows = 3;
    note.maxLength = notes.max_chars;
    note.placeholder = 'Write your message, or let us draft one.';
    var counter = el('span', 'gs-hint', '0/' + notes.max_chars);
    note.addEventListener('input', function () { g.note = note.value; counter.textContent = note.value.length + '/' + notes.max_chars; });
    noteWrap.appendChild(note);
    noteWrap.appendChild(counter);

    var toneRow = el('div', 'gs-chips gs-tones');
    (notes.tones || []).forEach(function (t) {
      var b = el('button', 'gs-chip gs-small-chip', t.label);
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
    name.placeholder = 'Their first name (optional)';
    name.addEventListener('input', function () { g.name = name.value.replace(/[^\p{L}\p{N} .'\-]/gu, ''); });
    noteWrap.appendChild(name);

    var draftBtn = el('button', 'gs-secondary gs-small', '✨ Write it for me');
    draftBtn.type = 'button';
    var draftStatus = el('span', 'gs-hint');
    draftStatus.setAttribute('role', 'status');
    draftBtn.addEventListener('click', function () {
      draftBtn.disabled = true;
      draftStatus.textContent = 'Writing…';
      fetch(ctx.api + '/note/draft', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ sid: ctx.sid, product_id: productId || null, recipient: state.answers.recipient,
          occasion: state.answers.occasion, tone: g.tone, name: g.name.trim() })
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, d: d }; });
      }).then(function (res) {
        if (!res.ok) {
          draftStatus.textContent = res.d.detail || 'Couldn’t write a note right now.';
          return;
        }
        g.note = note.value = res.d.note;
        track('note_drafted', productId);
        counter.textContent = note.value.length + '/' + notes.max_chars;
        g.left = res.d.rewrites_left;
        draftStatus.textContent = g.left > 0 ? 'Edit it any way you like. ' + g.left + ' rewrite' + (g.left === 1 ? '' : 's') + ' left.' : 'Edit it any way you like.';
        draftBtn.textContent = 'Rewrite';
        draftBtn.disabled = g.left === 0;
      }).catch(function () {
        draftStatus.textContent = 'Couldn’t write a note right now.';
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
    els.body.appendChild(el('p', 'gs-status', 'Add “' + pick.title + '” as a gift'));

    var labelWrap = el('label', 'gs-group');
    var label = el('input', 'gs-note');
    if (opts.askContext && !state.answers.recipient) {
      var intake = ctx.config.intake;
      els.body.appendChild(contextChips('Who\u2019s it for? (optional)', intake.recipients, 'recipient', function (v) {
        if (!label.value || label.value === g.label) { g.label = label.value = labelFor(v); }
      }));
      els.body.appendChild(contextChips('What\u2019s the occasion? (optional)', intake.occasions, 'occasion'));
    }
    var modes = el('fieldset', 'gs-group');
    modes.appendChild(el('legend', 'gs-label', 'Where is this going?'));
    var modeRow = el('div', 'gs-chips');
    [['direct', 'Ship it straight to them'], ['self', 'I’ll give it to them']].forEach(function (m) {
      var b = el('button', 'gs-chip', m[1]);
      b.type = 'button';
      b.setAttribute('data-value', m[0]);
      b.addEventListener('click', function () {
        g.mode = m[0];
        modeRow.querySelectorAll('.gs-chip').forEach(function (c) {
          c.setAttribute('aria-pressed', c.getAttribute('data-value') === g.mode ? 'true' : 'false');
        });
        labelWrap.hidden = g.mode !== 'self';
      });
      b.setAttribute('aria-pressed', m[0] === g.mode ? 'true' : 'false');
      modeRow.appendChild(b);
    });
    modes.appendChild(modeRow);
    els.body.appendChild(modes);

    labelWrap.appendChild(el('span', 'gs-label', 'Who’s it for?'));
    label.type = 'text';
    label.maxLength = 40;
    label.value = g.label;
    label.placeholder = 'e.g. Mom';
    label.addEventListener('input', function () { g.label = label.value; });
    labelWrap.appendChild(label);
    labelWrap.hidden = true;
    els.body.appendChild(labelWrap);

    els.body.appendChild(noteSection(g, pick.product_id));
    var wrapChoice = wrapSection(g);
    if (wrapChoice) els.body.appendChild(wrapChoice);
    var dateChoice = dateSection(g);
    if (dateChoice) els.body.appendChild(dateChoice);

    var row = el('div', 'gs-actions');
    var addBtn = el('button', 'gs-primary', 'Add gift to cart');
    addBtn.type = 'button';
    addBtn.addEventListener('click', function () { addGift(pick, variantId, g, addBtn); });
    row.appendChild(addBtn);
    var back = el('button', 'gs-secondary', opts.standalone ? 'Cancel' : 'Back to ideas');
    back.type = 'button';
    back.addEventListener('click', function () {
      if (opts.standalone) close(); else renderResults(state.lastPicks || [], false, true);
    });
    row.appendChild(back);
    els.body.appendChild(row);
    renderFoot();
    els.body.scrollTop = 0;
    var firstChip = els.body.querySelector('.gs-chip');
    if (firstChip) firstChip.focus();
  }

  // Gift wrap: merchant styles from /config. "No wrap" is the default (never
  // pre-select a paid add-on) and every style shows its price.
  // Arrive-by (Growth+): a date picker limited to what the store can make
  // (/config delivery.earliest..latest, store time). One date per order.
  function dateSection(g, existing) {
    var d = ctx.config.delivery;
    if (!d || !d.earliest) return null;
    var box = el('label', 'gs-group');
    box.appendChild(el('span', 'gs-label', 'When should it arrive? (optional)'));
    var input = el('input', 'gs-note gs-date');
    input.type = 'date';
    input.min = d.earliest;
    input.max = d.latest;
    if (existing && existing >= d.earliest && existing <= d.latest) input.value = g.arriveBy = existing;
    var hint = el('span', 'gs-hint gs-hint-left', 'Estimated delivery. Applies to the whole order.');
    input.addEventListener('change', function () {
      var v = input.value;
      if (v && (v < d.earliest || v > d.latest)) {
        hint.textContent = 'Please pick a date between ' + d.earliest + ' and ' + d.latest + '.';
        g.arriveBy = '';
        return;
      }
      g.arriveBy = v;
      hint.textContent = 'Estimated delivery. Applies to the whole order.';
    });
    box.appendChild(input);
    box.appendChild(hint);
    return box;
  }

  function wrapSection(g) {
    var styles = ctx.config.wrap || [];
    if (!styles.length) return null;
    g.wrap = null;
    var box = el('fieldset', 'gs-group');
    box.appendChild(el('legend', 'gs-label', 'Gift wrap (optional)'));
    var row = el('div', 'gs-chips');
    [null].concat(styles).forEach(function (w) {
      var b = el('button', 'gs-chip gs-small-chip', w ? w.name + ' · ' + (w.price ? '+' + money(w.price) : 'Free') : 'No wrap');
      b.type = 'button';
      b.setAttribute('aria-pressed', w === null ? 'true' : 'false');
      b.addEventListener('click', function () {
        g.wrap = w;
        row.querySelectorAll('.gs-chip').forEach(function (c) { c.setAttribute('aria-pressed', c === b ? 'true' : 'false'); });
      });
      row.appendChild(b);
    });
    box.appendChild(row);
    return box;
  }

  function addGift(pick, variantId, g, button) {
    var json = { 'Content-Type': 'application/json', Accept: 'application/json' };
    var label = (g.label || '').trim().slice(0, 40) || 'Gift';
    var note = (g.note || '').trim();
    button.disabled = true;
    button.textContent = 'Adding…';
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
      if (g.mode === 'self') {
        var group = groups.filter(function (x) { return (x.label || '').toLowerCase() === label.toLowerCase(); })[0];
        if (!group) {
          group = { id: 'g' + Math.random().toString(36).slice(2, 8), label: label };
          groups.push(group);
        }
        if (note) group.note = note;
        addWrap(group.id, label, group.wrap);
        if (g.wrap && !group.wrap) group.wrap = g.wrap.name;
        props['Gift for'] = label;
        props._giftsense_gift = group.id;
        update._giftsense_gifts = JSON.stringify(groups);
      } else {
        props._giftsense_gift = 'order';
        if (note) update['Gift note'] = note;
        addWrap('order', 'Your gift', attrs['Gift wrap']);
        if (g.wrap && !attrs['Gift wrap']) update['Gift wrap'] = g.wrap.name;
      }
      if (g.arriveBy) update['Arrive by'] = g.arriveBy;
      if (items.length > 1) track('wrap_added', pick.product_id);
      return fetch(root() + 'cart/add.js', {
        method: 'POST', credentials: 'same-origin', headers: json,
        body: JSON.stringify({ items: items })
      }).then(function (r) {
        if (!r.ok) throw new Error('add');
        return fetch(root() + 'cart/update.js', {
          method: 'POST', credentials: 'same-origin', headers: json, body: JSON.stringify({ attributes: update })
        });
      });
    }).then(function () {
      clear(els.body);
      track('panel_submit', pick.product_id);
      var done = el('p', 'gs-status', 'Added to your cart as a gift ✓');
      done.setAttribute('role', 'status');
      els.body.appendChild(done);
      var row = el('div', 'gs-actions');
      var view = el('a', 'gs-primary', 'View cart');
      view.href = root() + 'cart';
      row.appendChild(view);
      var more = el('button', 'gs-secondary', 'Keep looking');
      more.type = 'button';
      more.addEventListener('click', function () { renderResults(state.lastPicks || [], false, true); });
      row.appendChild(more);
      els.body.appendChild(row);
      renderFoot();
      view.focus();
      document.dispatchEvent(new CustomEvent('giftsense:added-to-cart', { detail: { variantId: variantId, gift: true } }));
    }).catch(function () {
      button.disabled = false;
      button.textContent = 'Try again';
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
    clear(els.body);
    if (!picks.length) {
      renderMessage(state.shown.length
        ? 'That\u2019s all we found for these answers. Try a different budget or occasion.'
        : 'We couldn\u2019t find a gift for that budget. Try another budget or occasion.');
      return;
    }
    var heading = el('p', 'gs-status' + (pending ? ' gs-status--pending' : ''),
      pending ? 'Here are some ideas \u00B7 personalizing\u2026' : 'Here are some ideas');
    heading.setAttribute('role', 'status');
    els.body.appendChild(heading);
    picks.forEach(function (p) {
      if (!pending && !restoring) state.shown.push(p.product_id);   // only final picks are excluded next time
      var href = safeHref(p.url);
      var card = el('div', 'gs-card');
      var link = el(href ? 'a' : 'div', 'gs-card-link');
      if (href) link.href = href;
      var src = sized(p.image_url, 240);
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
      if (href) name.href = href;
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
      els.body.appendChild(card);
    });
    if (!pending) state.lastPicks = picks;
    els.body.appendChild(actions(true, pending));
    renderFoot();
    // Move focus only on the first render, never when the AI picks swap in.
    if (!replacing) {
      var first = els.body.querySelector('.gs-card a');
      if (first && first.focus) first.focus();
    }
  }

  window.GiftSenseUI = { open: open, close: close };
})();
