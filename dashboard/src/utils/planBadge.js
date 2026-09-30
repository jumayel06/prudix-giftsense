/**
 * Sidebar plan badges, so merchants on lower plans see what they're missing.
 * Which plan includes a feature comes from /api/plans (app/config.py PLANS),
 * never hardcoded here.
 *
 *   included, built    → no badge
 *   included, planned  → "Soon"
 *   not included       → the lowest plan that has it ("Growth"), + "· Soon" if planned
 */
const ORDER = ['starter', 'growth', 'pro']

export function lowestPlanWith(plans, feature) {
  return [...plans].sort((a, b) => ORDER.indexOf(a.tier) - ORDER.indexOf(b.tier))
    .find(p => p.features.includes(feature)) || null
}

export function navBadge({ plans, planTier, feature, soon }) {
  const soonText = soon ? 'Soon' : null
  if (!plans || !feature) return soonText
  const mine = plans.find(p => p.tier === planTier)
  if (mine && mine.features.includes(feature)) return soonText
  const needed = lowestPlanWith(plans, feature)
  if (!needed) return soonText
  return { plan: needed.name, soon }
}
