/*
 * GiftSense bootstrap: loaded on every storefront page, so it stays tiny
 * (tests/unit/test_theme_extension.py keeps it under 10 KB). It draws the
 * launcher, reads the widget config once per browser session, and loads the
 * finder UI (giftsense-ui.js + giftsense.css) only on the first click.
 * No dependencies, no build step. Storefront calls go through the App Proxy.
 */
(function () {
  if (window.__giftsenseBoot) return;
  window.__giftsenseBoot = true;

  var CFG_KEY = 'giftsense:config';
  var CFG_TTL = 6 * 3600 * 1000;
  var CFG_OFF_TTL = 5 * 60 * 1000;
  var SID_KEY = 'giftsense:sid';
  var SID_TTL = 7 * 24 * 3600 * 1000;

  function store(kind) { try { return window[kind]; } catch (e) { return null; } }

  function readJson(kind, key) {
    var s = store(kind);
    try { return s ? JSON.parse(s.getItem(key) || 'null') : null; } catch (e) { return null; }
  }

  function writeJson(kind, key, value) {
    var s = store(kind);
    try { if (s) s.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode */ }
  }

  function uuid() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0;
      return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
  }

  // Widget session id: ties searches together for "show different ideas" and
  // (later) cart attribution. Never contains customer data.
  function sessionId() {
    var now = Date.now();
    var saved = readJson('localStorage', SID_KEY);
    if (saved && saved.id && saved.exp > now) return saved.id;
    var id = uuid();
    writeJson('localStorage', SID_KEY, { id: id, exp: now + SID_TTL });
    return id;
  }

  // Only real answers are cached. A failed request (backend restarting, proxy
  // hiccup) hides the launcher on this page only and is retried on the next,
  // instead of hiding it for the whole session. "Disabled" (plan inactive) is
  // re-checked after a few minutes so a newly chosen plan shows up quickly.
  function loadConfig(api) {
    var cached = readJson('sessionStorage', CFG_KEY);
    var ttl = cached && cached.data && cached.data.enabled ? CFG_TTL : CFG_OFF_TTL;
    if (cached && cached.t > Date.now() - ttl) return Promise.resolve(cached.data);
    return fetch(api + '/config', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) {
        if (!r.ok) return { enabled: false };
        return r.json().then(function (data) {
          writeJson('sessionStorage', CFG_KEY, { t: Date.now(), data: data });
          return data;
        });
      })
      .catch(function () { return { enabled: false }; });
  }

  var uiPromise = null;
  function loadUi(root) {
    if (uiPromise) return uiPromise;
    uiPromise = new Promise(function (resolve, reject) {
      var css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = root.getAttribute('data-css-src');
      document.head.appendChild(css);
      var js = document.createElement('script');
      js.src = root.getAttribute('data-ui-src');
      js.async = true;
      js.onload = function () { window.GiftSenseUI ? resolve(window.GiftSenseUI) : reject(new Error('ui')); };
      js.onerror = function () { uiPromise = null; reject(new Error('ui')); };
      document.head.appendChild(js);
    });
    return uiPromise;
  }

  function init() {
    var roots = document.querySelectorAll('[data-giftsense-root]');
    if (!roots.length) return;
    var main = roots[0];
    var api = main.getAttribute('data-api') || '/apps/giftsense';

    loadConfig(api).then(function (config) {
      if (!config || !config.enabled) {
        // Plan inactive: hide inline blocks too, rather than show a dead button.
        for (var i = 0; i < roots.length; i++) roots[i].setAttribute('hidden', '');
        return;
      }

      // Which kind of block opened the dialog: finder (default), a product
      // page (gift panel for this product) or the cart page (order note).
      function entryFor(trigger) {
        var r = trigger.closest('[data-giftsense-entry]');
        if (!r) return null;
        var entry = { type: r.getAttribute('data-giftsense-entry') };
        if (entry.type === 'product') {
          var form = document.querySelector('form[action*="/cart/add"] [name="id"]');
          entry.product = { product_id: r.getAttribute('data-product-id'), title: r.getAttribute('data-product-title'),
            variant_id: (form && form.value) || r.getAttribute('data-variant-id') };
        }
        return entry;
      }

      function open(trigger) {
        trigger.setAttribute('aria-busy', 'true');
        loadUi(main).then(function (ui) {
          trigger.removeAttribute('aria-busy');
          ui.open({
            entry: entryFor(trigger),
            api: api, config: config, sid: sessionId(), trigger: trigger,
            currency: main.getAttribute('data-currency') || 'USD',
            locale: main.getAttribute('data-locale') || document.documentElement.lang || 'en',
            accent: getComputedStyle(main).getPropertyValue('--gs-accent') || '#111827',
            onAccent: getComputedStyle(main).getPropertyValue('--gs-on-accent') || '#ffffff'
          });
        }).catch(function () { trigger.removeAttribute('aria-busy'); });
      }

      var embed = document.getElementById('giftsense-embed');
      if (embed) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'giftsense-launcher giftsense-launcher--' +
          (embed.getAttribute('data-position') === 'left' ? 'left' : 'right');
        btn.setAttribute('aria-haspopup', 'dialog');
        btn.style.setProperty('--gs-accent', getComputedStyle(embed).getPropertyValue('--gs-accent'));
        btn.style.setProperty('--gs-on-accent', getComputedStyle(embed).getPropertyValue('--gs-on-accent'));
        btn.textContent = '🎁 ' + (embed.getAttribute('data-label') || 'Find a gift');
        btn.addEventListener('click', function () { open(btn); });
        document.body.appendChild(btn);
      }

      var buttons = document.querySelectorAll('[data-giftsense-open]');
      for (var j = 0; j < buttons.length; j++) {
        (function (b) { b.addEventListener('click', function () { open(b); }); })(buttons[j]);
      }
    }).catch(function () { /* network error: stay invisible, never break the page */ });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
