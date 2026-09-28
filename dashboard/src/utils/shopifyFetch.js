/**
 * Authenticated fetch wrapper for Shopify embedded app API calls.
 *
 * Production (inside Shopify Admin):
 *   - Reads a short-lived session token from App Bridge (window.shopify.idToken)
 *   - Attaches it as  Authorization: Bearer <token>
 *   - Backend verifies via PyJWT and resolves the shop from the token
 *
 * Local development (no Shopify context):
 *   - Falls back to appending ?shop=<domain> to the URL
 *   - Backend accepts the ?shop= query param as a fallback
 */

function _devShop() {
  const params = new URLSearchParams(window.location.search)
  return params.get('shop') || import.meta.env.VITE_SHOP || 'prudix-commerce-dev.myshopify.com'
}

/**
 * Like shopifyFetch but throws on non-2xx responses.
 * Use this for data-loading calls where any error should abort the operation.
 * The thrown error has a `.status` property with the HTTP status code.
 */
export async function fetchJson(url, options = {}) {
  const res = await shopifyFetch(url, options)
  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try {
      const body = await res.json()
      message = body.detail || message
    } catch { /* non-JSON error body */ }
    const err = new Error(message)
    err.status = res.status
    throw err
  }
  return res.json()
}

export async function shopifyFetch(url, options = {}) {
  const hasAppBridge =
    typeof window !== 'undefined' &&
    typeof window.shopify !== 'undefined' &&
    typeof window.shopify.idToken === 'function'

  if (!hasAppBridge) {
    // Local dev: append ?shop= so the backend ?shop= fallback works
    const shop = _devShop()
    if (shop) {
      const sep = url.includes('?') ? '&' : '?'
      url = `${url}${sep}shop=${encodeURIComponent(shop)}`
    }
    return fetch(url, options)
  }

  const send = async () => {
    const headers = { ...(options.headers || {}) }
    try {
      const token = await window.shopify.idToken()
      headers['Authorization'] = `Bearer ${token}`
    } catch (e) {
      console.warn('[shopifyFetch] Could not get session token:', e)
    }
    return fetch(url, { ...options, headers })
  }

  // Session tokens live for only 60s. If we hit the exp edge (or App Bridge
  // handed us a token about to expire), retry once with a freshly-issued
  // token before surfacing the 401 to callers. This makes intermittent
  // "Invalid session token" 401s invisible on the happy path.
  const res = await send()
  if (res.status !== 401) return res
  return send()
}
