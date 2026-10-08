/**
 * Sidebar badges, matching Commerce: a lock on features the merchant's plan
 * lacks (no plan names), "Soon" on planned ones. Which plan includes a feature
 * comes from /api/plans (app/config.py PLANS), never hardcoded here.
 * Returns "locked" when the merchant's plan lacks `feature` (shown as 🔒; the
 * item opens Plans), else null. Planned items get "Soon" in App.jsx.
 */
export function navBadge({ plans, planTier, feature }) {
  if (!plans || !feature) return null
  const mine = plans.find(p => p.tier === planTier)
  if (mine && mine.features.includes(feature)) return null
  return plans.some(p => p.features.includes(feature)) ? 'locked' : null
}
