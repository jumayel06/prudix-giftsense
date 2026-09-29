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
  var state = { answers: { recipient: null, occasion: null, budget_band: null, vibes: [], free_text: '' }, shown: [] };
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

  function safeHref(url) { return typeof url === 'string' && url.charAt(0) === '/' && url.charAt(1) !== '/' ? url : null; }

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
    renderIntake();
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
    els.go.addEventListener('click', function () { state.shown = []; search(); });
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
  function skeleton() {
    clear(els.body);
    var status = el('p', 'gs-status', 'Finding gifts…');
    status.setAttribute('role', 'status');
    els.body.appendChild(status);
    for (var i = 0; i < 3; i++) {
      var card = el('div', 'gs-card gs-card--skeleton');
      card.appendChild(el('div', 'gs-img'));
      card.appendChild(el('div', 'gs-lines'));
      els.body.appendChild(card);
    }
  }

  function search() {
    skeleton();
    var a = state.answers;
    var body = {
      sid: ctx.sid, recipient: a.recipient, occasion: a.occasion, budget_band: a.budget_band,
      vibes: a.vibes.slice(), free_text: a.free_text.trim(), exclude_ids: state.shown.slice(-MAX_EXCLUDE)
    };
    if (AGE_FOR[a.recipient]) body.age_band = AGE_FOR[a.recipient];
    fetch(ctx.api + '/search', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) { return { ok: r.ok, status: r.status, data: data }; });
    }).then(function (res) {
      if (!res.ok) {
        renderMessage(res.status === 429 && res.data.detail ? res.data.detail
          : 'Something went wrong. Please try again.');
        return;
      }
      renderResults(res.data.picks || []);
    }).catch(function () { renderMessage('Something went wrong. Please check your connection and try again.'); });
  }

  function actions(showMore) {
    var row = el('div', 'gs-actions');
    if (showMore) {
      var more = el('button', 'gs-primary', 'Show different ideas');
      more.type = 'button';
      more.addEventListener('click', search);
      row.appendChild(more);
    }
    var back = el('button', 'gs-secondary', 'Change answers');
    back.type = 'button';
    back.addEventListener('click', function () { renderIntake(); });
    row.appendChild(back);
    return row;
  }

  function renderMessage(text) {
    clear(els.body);
    var p = el('p', 'gs-status', text);
    p.setAttribute('role', 'alert');
    els.body.appendChild(p);
    els.body.appendChild(actions(false));
    renderFoot();
  }

  function renderResults(picks) {
    clear(els.body);
    if (!picks.length) {
      renderMessage(state.shown.length
        ? 'That’s all we found for these answers. Try a different budget or occasion.'
        : 'We couldn’t find a gift for that budget. Try another budget or occasion.');
      return;
    }
    var heading = el('p', 'gs-status', 'Here are some ideas');
    heading.setAttribute('role', 'status');
    els.body.appendChild(heading);
    picks.forEach(function (p) {
      state.shown.push(p.product_id);
      var href = safeHref(p.url);
      var card = el(href ? 'a' : 'div', 'gs-card');
      if (href) card.href = href;
      var src = sized(p.image_url, 240);
      if (src) {
        var img = el('img', 'gs-img');
        img.src = src;
        img.alt = '';
        img.loading = 'lazy';
        card.appendChild(img);
      } else {
        card.appendChild(el('div', 'gs-img'));
      }
      var text = el('div', 'gs-text');
      text.appendChild(el('span', 'gs-name', p.title));
      text.appendChild(el('span', 'gs-price', p.price_min === p.price_max
        ? money(p.price_min) : money(p.price_min) + ' – ' + money(p.price_max)));
      text.appendChild(el('span', 'gs-reason', p.reason));
      card.appendChild(text);
      els.body.appendChild(card);
    });
    els.body.appendChild(actions(true));
    renderFoot();
    var first = els.body.querySelector('.gs-card');
    if (first && first.focus) first.focus();
  }

  window.GiftSenseUI = { open: open, close: close };
})();
