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

  // Widget session id (searches + cart attribution). No customer data.
  function sessionId() {
    var now = Date.now();
    var saved = readJson('localStorage', SID_KEY);
    if (saved && saved.id && saved.exp > now) return saved.id;
    var id = uuid();
    writeJson('localStorage', SID_KEY, { id: id, exp: now + SID_TTL });
    return id;
  }

  // Only real answers are cached; a failure hides the launcher on this page
  // only. "Disabled" is re-checked after a few minutes.
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
  // Widget text in the storefront's language, if shipped (else English).
  function loadText(root) {
    var src = root.getAttribute('data-i18n-src');
    if (!src || window.GiftSenseI18n) return Promise.resolve();
    return new Promise(function (resolve) {
      var js = document.createElement('script');
      js.src = src;
      js.async = true;
      js.onload = js.onerror = function () { resolve(); };
      document.head.appendChild(js);
    });
  }

  function loadUi(root) {
    if (uiPromise) return uiPromise;
    var text = loadText(root);
    uiPromise = new Promise(function (resolve, reject) {
      var css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = root.getAttribute('data-css-src');
      document.head.appendChild(css);
      var js = document.createElement('script');
      js.src = root.getAttribute('data-ui-src');
      js.async = true;
      js.onload = function () {
        if (!window.GiftSenseUI) { reject(new Error('ui')); return; }
        text.then(function () { resolve(window.GiftSenseUI); });
      };
      js.onerror = function () { uiPromise = null; reject(new Error('ui')); };
      document.head.appendChild(js);
    });
    return uiPromise;
  }

  // Wrap cart guard (giftsense-cart.js): only where wrap was added.
  function armCartGuard(root) {
    var src = root.getAttribute('data-cart-src');
    if (!src || window.__giftsenseCartGuard) return;
    var js = document.createElement('script');
    js.src = src;
    js.async = true;
    document.head.appendChild(js);
  }

  // Another app's floating button (chat, AI concierge…) in the same corner:
  // look under our launcher (it's on top) and sit just above any fixed element.
  function fixedAncestor(n, own) {
    for (; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      if (n === own) return null;
      if (getComputedStyle(n).position === 'fixed') return n;
    }
    return null;
  }

  function avoidOverlap(btn) {
    if (!document.elementsFromPoint) return;
    function check() {
      btn.style.transform = '';
      var r = btn.getBoundingClientRect(), top = Infinity;
      [[r.left + 4, r.bottom - 4], [r.right - 4, r.bottom - 4], [r.left + r.width / 2, r.top + r.height / 2]].forEach(function (p) {
        document.elementsFromPoint(p[0], p[1]).forEach(function (n) {
          var f = fixedAncestor(n, btn);
          if (f && f.offsetWidth < window.innerWidth * 0.6) top = Math.min(top, f.getBoundingClientRect().top);
        });
      });
      if (top !== Infinity) btn.style.transform = 'translateY(' + -Math.ceil(r.bottom - top + 12) + 'px)';
    }
    [1500, 4000, 8000].forEach(function (ms) { setTimeout(check, ms); });
    var t;
    window.addEventListener('resize', function () { clearTimeout(t); t = setTimeout(check, 300); });
  }

  function init() {
    var roots = document.querySelectorAll('[data-giftsense-root]');
    if (!roots.length) return;
    var main = roots[0];
    window.GiftSenseArmCart = function () { armCartGuard(main); };
    try { if (localStorage.getItem('giftsense:wrap')) armCartGuard(main); } catch (e) { /* private mode */ }
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
            variant_id: (form && form.value) || r.getAttribute('data-variant-id'), wrap: !r.hasAttribute('data-no-wrap') };
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

      // Cart drawer (giftsense-drawer.js): only on pages that have one.
      window.__giftsenseConfig = config;
      window.GiftSenseOpen = open;
      var embed = document.getElementById('giftsense-embed');
      if (embed && embed.getAttribute('data-drawer-src') &&
          document.querySelector('cart-drawer,#CartDrawer,#cart-drawer,.cart-drawer,[data-cart-drawer],#sidebar-cart,.drawer--cart,#mini-cart,.mini-cart')) {
        var dj = document.createElement('script');
        dj.src = embed.getAttribute('data-drawer-src');
        dj.async = true;
        document.head.appendChild(dj);
      }
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
        var lift = parseInt(embed.getAttribute('data-offset'), 10) || 0;
        if (lift) btn.style.bottom = 'calc(' + getComputedStyle(btn).bottom + ' + ' + lift + 'px)';
        document.body.appendChild(btn);
        avoidOverlap(btn);
      }

      // "Add to registry" (Pro); the work happens in the lazily loaded UI.
      var regButtons = config.registry ? document.querySelectorAll('[data-giftsense-registry]') : [];
      for (var k = 0; k < regButtons.length; k++) (function (b) {
        b.hidden = false;
        b.addEventListener('click', function () {
          if (!b.getAttribute('data-logged-in')) { location.href = b.getAttribute('data-login-url'); return; }
          loadUi(main).then(function (ui) { ui.addToRegistry(b, api); });
        });
      })(regButtons[k]);

      var buttons = document.querySelectorAll('[data-giftsense-open]');
      for (var j = 0; j < buttons.length; j++) {
        (function (b) { b.addEventListener('click', function () { open(b); }); })(buttons[j]);
      }
    }).catch(function () { /* network error: stay invisible, never break the page */ });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
