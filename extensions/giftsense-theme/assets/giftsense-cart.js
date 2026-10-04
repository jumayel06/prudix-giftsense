/*
 * GiftSense wrap cart guard. A wrap line (property _giftsense_wrap_for = gift
 * group id) is removed when the last item of its gift group (_giftsense_gift)
 * leaves the cart. Loaded by the bootstrap (giftsense.js) only in browsers
 * that added wrap (localStorage "giftsense:wrap", set by giftsense-ui.js), so
 * other shoppers never download it. Checks on load and after the theme
 * changes the cart through fetch or XHR (cart page, drawers, quantity pickers).
 */
(function () {
  if (window.__giftsenseCartGuard) return;
  window.__giftsenseCartGuard = true;

  var WRAP_KEY = 'giftsense:wrap';
  var busy = false, timer = null;

  function root() { return (window.Shopify && Shopify.routes && Shopify.routes.root) || '/'; }

  function cartJson(path, body) {
    var opts = { credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (body) {
      opts.method = 'POST';
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(root() + path, opts).then(function (r) { return r.json(); });
  }

  function addonFor(it) {
    var p = it.properties || {};
    return p._giftsense_wrap_for || p._giftsense_card_for;
  }

  // Orphaned wrap/card lines: for a group that has no gift items left. The
  // whole-order wrap ("order", from the cart page / drawer or a direct gift)
  // stays while anything that isn't wrap is still in the cart.
  function orphans(items) {
    var live = {};
    items.forEach(function (it) {
      var p = it.properties || {};
      if (p._giftsense_wrap_for || p._giftsense_card_for) return;
      if (p._giftsense_gift) live[p._giftsense_gift] = true;
      live.order = true;
    });
    return items.filter(function (it) {
      var g = addonFor(it);
      return g && !live[g];
    });
  }

  function attributeUpdate(attrs, gone) {
    var update = {};
    if (gone.order && attrs['Gift wrap']) update['Gift wrap'] = '';
    if (gone.order && attrs['Greeting card']) update['Greeting card'] = '';
    try {
      var groups = JSON.parse(attrs._giftsense_gifts || '[]');
      if (Array.isArray(groups) && groups.some(function (x) { return gone[x.id] && (x.wrap || x.card); })) {
        groups.forEach(function (x) { if (gone[x.id]) { delete x.wrap; delete x.card; } });
        update._giftsense_gifts = JSON.stringify(groups);
      }
    } catch (e) { /* malformed attribute: leave it */ }
    return update;
  }

  function refreshCartUi() {
    // Themes refresh their cart UI differently: the cart page reloads, drawers
    // get the common refresh events.
    if (/\/cart\/?$/.test(location.pathname)) { location.reload(); return; }
    ['cart:refresh', 'cart:updated'].forEach(function (n) { document.dispatchEvent(new CustomEvent(n)); });
  }

  function check() {
    if (busy) return;
    busy = true;
    cartJson('cart.js').then(function (cart) {
      var items = (cart && cart.items) || [];
      if (!items.some(addonFor)) {
        try { localStorage.removeItem(WRAP_KEY); } catch (e) { /* private mode */ }
        return;
      }
      var lines = orphans(items);
      if (!lines.length) return;
      var gone = {};
      var chain = Promise.resolve();
      lines.forEach(function (it) {
        gone[addonFor(it)] = true;
        chain = chain.then(function () { return cartJson('cart/change.js', { id: it.key, quantity: 0 }); });
      });
      return chain.then(function () {
        var update = attributeUpdate(cart.attributes || {}, gone);
        return Object.keys(update).length ? cartJson('cart/update.js', { attributes: update }) : null;
      }).then(refreshCartUi);
    }).catch(function () { /* never break the page */ }).then(function () { busy = false; });
  }

  function schedule(url) {
    if (busy || !/\/cart\/(change|update|clear)/.test(String(url || ''))) return;
    clearTimeout(timer);
    timer = setTimeout(check, 400);
  }

  var f = window.fetch;
  window.fetch = function (input) {
    var p = f.apply(this, arguments);
    var url = input && input.url ? input.url : input;
    p.then(function () { schedule(url); }, function () {});
    return p;
  };
  var open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.addEventListener('loadend', function () { schedule(url); });
    return open.apply(this, arguments);
  };

  check();
})();
