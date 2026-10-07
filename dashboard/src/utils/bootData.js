// Data the App shell already loaded on startup, handed to the first page that
// needs it so the plan picker doesn't wait on a second round trip at install.
//
// - Plans: public, static per deploy. Fetched once, in parallel with the
//   shell's /api/stats call. A failed fetch isn't cached (next caller retries).
// - Stats: the shell's /api/stats response, used ONCE by the next plan-picker
//   mount and only if under a minute old; any later visit fetches fresh.

let plansPromise = null
let bootStats = null
let bootStatsAt = 0
const BOOT_STATS_MAX_AGE_MS = 60_000

export function prefetchPlans() {
  if (!plansPromise) {
    plansPromise = fetch('/api/plans')
      .then(r => (r.ok ? r.json() : null))
      .catch(() => null)
      .then(data => {
        if (!data) plansPromise = null
        return data
      })
  }
  return plansPromise
}

export function rememberBootStats(stats) {
  bootStats = stats || null
  bootStatsAt = Date.now()
}

export function takeBootStats() {
  const s = bootStats
  bootStats = null
  return s && Date.now() - bootStatsAt < BOOT_STATS_MAX_AGE_MS ? s : null
}
