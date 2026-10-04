/*
 * GiftSense in the theme's cart drawer: an "Add a gift note" button above the
 * drawer's checkout button that opens the same panel as the cart page (note,
 * whole-order wrap, arrive-by date). Loaded by the bootstrap (giftsense.js)
 * only on pages that have a cart drawer. Themes re-render their drawer after
 * every cart change, so the button is put back when it disappears. Unknown
 * drawer markup: nothing happens.
 */
(function () {
  if (window.__giftsenseDrawer) return;
  window.__giftsenseDrawer = true;

  var DRAWERS = 'cart-drawer, #CartDrawer, #cart-drawer, .cart-drawer, [data-cart-drawer], #sidebar-cart, .drawer--cart, #mini-cart, .mini-cart';
  var CHECKOUT = '[name="checkout"], #CartDrawer-Checkout, .cart__checkout-button, a[href$="/checkout"], button[onclick*="checkout"]';
  var CLOSE = '[aria-label*="close" i], .drawer__close, [data-drawer-close], .cart-drawer__close';
  var root = document.getElementById('giftsense-embed');
  if (!root) return;

  function label() {
    var config = window.__giftsenseConfig || {};
    return root.getAttribute((config.wrap || []).length ? 'data-drawer-label-wrap' : 'data-drawer-label') || 'Add a gift note';
  }

  function closeDrawer(drawer) {
    // Drawers trap focus; close it so the shopper can type in our panel.
    if (typeof drawer.close === 'function') { try { drawer.close(); return; } catch (e) { /* fall through */ } }
    var x = drawer.querySelector(CLOSE);
    if (x) x.click();
  }

  function place(drawer) {
    if (drawer.querySelector('.giftsense-drawer, [data-giftsense-root]')) return;
    var checkout = drawer.querySelector(CHECKOUT);
    if (!checkout || !checkout.parentNode) return;
    var box = document.createElement('div');
    box.className = 'giftsense-drawer';
    box.setAttribute('data-giftsense-entry', 'cart');
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'giftsense-drawer__button';
    b.textContent = '🎁 ' + label();
    b.addEventListener('click', function () {
      closeDrawer(drawer);
      if (window.GiftSenseOpen) window.GiftSenseOpen(b);
    });
    box.appendChild(b);
    checkout.parentNode.insertBefore(box, checkout);
  }

  function watch(drawer) {
    place(drawer);
    var t;
    new MutationObserver(function () {
      clearTimeout(t);
      t = setTimeout(function () { place(drawer); }, 150);
    }).observe(drawer, { childList: true, subtree: true });
  }

  var style = document.createElement('style');
  style.textContent = '.giftsense-drawer{margin:0 0 10px;width:100%}' +
    '.giftsense-drawer__button{width:100%;padding:11px 16px;border:1px solid currentColor;border-radius:999px;' +
    'background:transparent;color:inherit;font:inherit;font-weight:600;cursor:pointer}' +
    '.giftsense-drawer__button:focus-visible{outline:2px solid currentColor;outline-offset:2px}';
  document.head.appendChild(style);
  Array.prototype.forEach.call(document.querySelectorAll(DRAWERS), watch);
})();
