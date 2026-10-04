/**
 * Dashboard sections that are planned but not built yet. They show in the
 * sidebar with a "Soon" badge and open ComingSoonPage. Feature keys match
 * `features` in /api/plans (app/config.py), so "which plans include it" is
 * never hardcoded here. Remove an entry when its real page ships.
 */
export const UPCOMING = []

export const UPCOMING_BY_PATH = Object.fromEntries(UPCOMING.map(u => [u.path, u]))
