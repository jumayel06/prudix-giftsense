/**
 * Parse a FastAPI error response into a human-readable message and
 * an optional structured payload for plan upgrade CTAs.
 *
 * Returns { message, code, upgradeTo }
 *   - message   : string to display
 *   - code      : "generation_limit_exceeded" | "feature_not_available" | null
 *   - upgradeTo : plan tier string when relevant, or null
 */
export function parseApiError(json, fallback = 'Something went wrong. Please try again.') {
  const detail = json?.detail
  if (!detail) return { message: fallback, code: null, upgradeTo: null, isTrial: false, resumesAt: null }

  if (typeof detail === 'string') {
    return { message: detail, code: null, upgradeTo: null, isTrial: false, resumesAt: null }
  }

  if (typeof detail === 'object') {
    return {
      message: detail.message || fallback,
      code: detail.code || null,
      upgradeTo: detail.upgrade_to || null,
      isTrial: !!detail.is_trial,
      resumesAt: detail.resumes_at || null,
    }
  }

  return { message: fallback, code: null, upgradeTo: null, isTrial: false, resumesAt: null }
}

/** Convenience: fetch + parse error in one call. Throws a structured error on non-2xx. */
export async function apiFetch(url, options) {
  const res = await fetch(url, options)
  if (res.ok) return res.json()
  let json = {}
  try { json = await res.json() } catch { /* non-JSON error body */ }
  const { message, code, upgradeTo } = parseApiError(json)
  const err = new Error(message)
  err.code = code
  err.upgradeTo = upgradeTo
  err.status = res.status
  throw err
}
