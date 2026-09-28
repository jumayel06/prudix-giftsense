/**
 * Thin wrapper around the App Bridge global toast API (`window.shopify.toast`).
 * Falls back to a no-op in non-embedded contexts so dev/standalone testing
 * doesn't blow up.
 *
 * Usage:
 *   import { showToast } from '../utils/toast'
 *   showToast('Industry saved.')
 *   showToast('Could not save.', { isError: true })
 *   showToast('Curating...', { duration: 8000 })
 */
export function showToast(message, opts = {}) {
  if (!message) return
  try {
    if (window.shopify && window.shopify.toast && typeof window.shopify.toast.show === 'function') {
      window.shopify.toast.show(message, {
        duration: opts.duration ?? 4000,
        isError: !!opts.isError,
      })
      return
    }
  } catch (e) {
    console.warn('[toast] App Bridge toast failed:', e)
  }
  // Non-embedded fallback — log to console so devs aren't left wondering
  // why nothing happened.
  if (opts.isError) console.error('[toast]', message)
  else console.log('[toast]', message)
}
