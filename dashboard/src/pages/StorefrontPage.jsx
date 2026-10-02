import { useState, useEffect, useCallback } from 'react'
import { Page, Layout, Card, BlockStack, InlineStack, Text, Button, Banner, Badge, List, SkeletonBodyText } from '@shopify/polaris'
import { fetchJson } from '../utils/shopifyFetch'
import { EMBED_HANDLE, BLOCK_HANDLE, OPTIONS_HANDLE, openThemeEditor } from '../utils/themeEditor'

const EMBED_BADGE = {
  on: <Badge tone="success">On</Badge>,
  off: <Badge tone="warning">Switched off</Badge>,
  missing: <Badge tone="attention">Not added yet</Badge>,
}

// "Added: Home page, Collection pages" from /api/theme/status blocks.
function PlacedBadge({ pages }) {
  if (!pages) return <Badge>Optional</Badge>
  return pages.length
    ? <Badge tone="success">{`Added: ${pages.join(', ')}`}</Badge>
    : <Badge>Not added yet</Badge>
}

function Step({ number, title, children, action }) {
  return (
    <Card>
      <InlineStack gap="400" blockAlign="start" wrap={false}>
        <div style={{
          flex: '0 0 32px', height: 32, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'var(--p-color-bg-fill-brand)', color: 'var(--p-color-text-brand-on-bg-fill)', fontWeight: 600,
        }}>{number}</div>
        <BlockStack gap="200">
          <Text as="h2" variant="headingMd">{title}</Text>
          {children}
          {action && <div>{action}</div>}
        </BlockStack>
      </InlineStack>
    </Card>
  )
}

export default function StorefrontPage() {
  const [shop, setShop] = useState(null)
  const [error, setError] = useState(null)
  const [theme, setTheme] = useState(null)
  const [checking, setChecking] = useState(false)
  const apiKey = import.meta.env.VITE_SHOPIFY_API_KEY

  const loadTheme = () => fetchJson('/api/theme/status').then(setTheme).catch(() => setTheme({ embed: 'unknown' }))

  const checkTheme = useCallback(() => {
    setChecking(true)
    loadTheme().finally(() => setChecking(false))
  }, [])

  useEffect(() => {
    fetchJson('/api/settings').then(d => setShop(d.shop_domain)).catch(() => setError('Could not load your store details.'))
    loadTheme()
  }, [])

  const embedOn = theme?.embed === 'on'
  const finderPages = theme?.blocks?.[BLOCK_HANDLE] || []
  const optionsPages = theme?.blocks?.[OPTIONS_HANDLE] || []

  // Theme editor lives outside the embedded app: open it in the top window.
  const openEditor = params => openThemeEditor(shop, params)

  if (!shop) {
    return (
      <Page title="Storefront">
        {error ? <Banner tone="critical" title={error} /> : <Card><SkeletonBodyText lines={6} /></Card>}
      </Page>
    )
  }

  return (
    <Page title="Storefront" subtitle="Put the gift finder in front of your shoppers.">
      <Layout>
        <Layout.Section>
          <BlockStack gap="400">
            <Step
              number="1"
              title={<InlineStack gap="200" blockAlign="center"><span>Turn on the gift finder button</span>{EMBED_BADGE[theme?.embed]}</InlineStack>}
              action={<InlineStack gap="200">
                <Button variant={embedOn ? 'secondary' : 'primary'} onClick={() => openEditor({ context: 'apps', activateAppId: `${apiKey}/${EMBED_HANDLE}` })}>
                  {embedOn ? 'Customize in theme editor' : 'Turn on in theme editor'}
                </Button>
                <Button variant="plain" onClick={checkTheme} loading={checking}>Check again</Button>
              </InlineStack>}
            >
              {embedOn ? (
                <Text as="p">
                  The <b>Find a gift</b> button is live on your store{theme.theme_name ? ` (theme: ${theme.theme_name})` : ''}.
                  You can change its text, corner and colors in the theme editor.
                </Text>
              ) : (
                <>
                  <Text as="p">
                    Adds a floating <b>Find a gift</b> button to every page of your store. In the theme editor, make sure
                    <b> GiftSense gift finder</b> is switched on under App embeds, then click <b>Save</b>.
                  </Text>
                  <Text as="p" tone="subdued">You can change the button text, corner and colors there too. Come back and click Check again.</Text>
                </>
              )}
            </Step>

            <Step
              number="2"
              title={<InlineStack gap="200" blockAlign="center"><span>Add a gift finder section</span>
                <PlacedBadge pages={theme?.blocks?.[BLOCK_HANDLE]} /></InlineStack>}
              action={<InlineStack gap="200">
                <Button onClick={() => openEditor({ template: 'index', addAppBlockId: `${apiKey}/${BLOCK_HANDLE}`, target: 'newAppsSection' })}>
                  {finderPages.includes('Home page') ? 'Add to another page' : 'Add to home page'}
                </Button>
                <Button variant="plain" onClick={checkTheme} loading={checking}>Check again</Button>
              </InlineStack>}
            >
              <Text as="p">
                A <b>Not sure what to get?</b> section with a button that opens the gift finder. Great on your home page,
                gift collections and holiday pages. Optional: the floating button already works on every page.
              </Text>
            </Step>

            <Step
              number="3"
              title={<InlineStack gap="200" blockAlign="center"><span>Add gift options to product and cart pages</span>
                <PlacedBadge pages={theme?.blocks?.[OPTIONS_HANDLE]} /></InlineStack>}
              action={<InlineStack gap="200">
                <Button onClick={() => openEditor({ template: 'product', addAppBlockId: `${apiKey}/${OPTIONS_HANDLE}`, target: 'mainSection' })}
                  disabled={optionsPages.includes('Product pages')}>
                  {optionsPages.includes('Product pages') ? 'On product pages' : 'Add to product page'}
                </Button>
                <Button onClick={() => openEditor({ template: 'cart', addAppBlockId: `${apiKey}/${OPTIONS_HANDLE}`, target: 'mainSection' })}
                  disabled={optionsPages.includes('Cart page')}>
                  {optionsPages.includes('Cart page') ? 'On cart page' : 'Add to cart page'}
                </Button>
              </InlineStack>}
            >
              <Text as="p">
                Lets shoppers who find a gift on their own still mark it as a gift: <b>This is a gift</b> on product pages
                (who it&apos;s for, an AI-written note, gift wrap) and <b>Add a gift note</b> on the cart page.
              </Text>
            </Step>

            <Step number="4" title="Try it on your store">
              <Text as="p">Open your store, click <b>Find a gift</b>, answer a few questions and see the picks.</Text>
              <List>
                <List.Item>Picks come only from products the gift finder has analyzed (see Catalog).</List.Item>
                <List.Item>Each search uses generations based on your AI option in Settings. If you run out, shoppers still get picks with simpler reasons.</List.Item>
              </List>
              <div>
                <Button url={`https://${shop}`} target="_blank">Open your store</Button>
              </div>
            </Step>
          </BlockStack>
        </Layout.Section>
      </Layout>
    </Page>
  )
}
