import { useState, useEffect } from 'react'
import { Banner } from '@shopify/polaris'
import { fetchJson } from '../utils/shopifyFetch'
import { openEmbedSwitch } from '../utils/themeEditor'

/**
 * Shown on every page when GiftSense isn't switched on in the live theme,
 * typically after the merchant published a new theme (app embeds and sections
 * are saved per theme). On mount it re-checks the theme once, so it clears
 * itself if the merchant already fixed it in the theme editor.
 */
export default function ThemeWarningBanner({ warning, shop, onNavigate }) {
  const [current, setCurrent] = useState(warning)
  const [checking, setChecking] = useState(false)
  const apiKey = import.meta.env.VITE_SHOPIFY_API_KEY

  const recheck = () => {
    setChecking(true)
    return fetchJson('/api/theme/status')
      .then(r => { if (r.embed === 'on') setCurrent(null) })
      .catch(() => {})
      .finally(() => setChecking(false))
  }

  // Quiet re-check on mount (no "Checking…"): clears a stale warning.
  useEffect(() => {
    fetchJson('/api/theme/status').then(r => { if (r.embed === 'on') setCurrent(null) }).catch(() => {})
  }, [])

  if (!current || !shop) return null
  const theme = current.theme_name ? `“${current.theme_name}”` : 'your live theme'
  const title = current.theme_changed
    ? `The gift finder isn't on your new theme ${theme}`
    : `The gift finder isn't switched on in ${theme}`

  return (
    <div style={{ margin: '0 auto 16px', maxWidth: 998, padding: '0 16px' }}>
      <Banner
        tone="warning"
        title={title}
        action={{ content: 'Turn on in theme editor', onAction: () => openEmbedSwitch(shop, apiKey) }}
        secondaryAction={{ content: checking ? 'Checking…' : 'I turned it on', onAction: recheck }}
      >
        <p>
          Shoppers can&apos;t see the Find a gift button right now. Switch on <b>GiftSense gift finder</b> under App embeds,
          then click Save.
          {current.blocks_lost && (
            <> Your Find a gift section and Gift options also need adding to this theme: see{' '}
              <a href="#" onClick={e => { e.preventDefault(); onNavigate('/storefront') }}>Storefront</a>.</>
          )}
        </p>
      </Banner>
    </div>
  )
}
