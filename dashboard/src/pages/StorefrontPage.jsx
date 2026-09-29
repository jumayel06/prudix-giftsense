import { useState, useEffect } from 'react'
import { Page, Layout, Card, BlockStack, InlineStack, Text, Button, Banner, Badge, List, SkeletonBodyText } from '@shopify/polaris'
import { fetchJson } from '../utils/shopifyFetch'

// Theme app extension handles (extensions/giftsense-theme/blocks/*.liquid).
const EMBED_HANDLE = 'app-embed'
const BLOCK_HANDLE = 'gift-finder'

function editorUrl(shop, params) {
  return `https://${shop}/admin/themes/current/editor?${new URLSearchParams(params)}`
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
  const apiKey = import.meta.env.VITE_SHOPIFY_API_KEY

  useEffect(() => {
    fetchJson('/api/settings').then(d => setShop(d.shop_domain)).catch(() => setError('Could not load your store details.'))
  }, [])

  // Theme editor lives outside the embedded app: open it in the top window.
  const openEditor = params => window.open(editorUrl(shop, params), '_top')

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
              title="Turn on the gift finder button"
              action={<Button variant="primary" onClick={() => openEditor({ context: 'apps', activateAppId: `${apiKey}/${EMBED_HANDLE}` })}>
                Turn on in theme editor
              </Button>}
            >
              <Text as="p">
                Adds a floating <b>Find a gift</b> button to every page of your store. In the theme editor, make sure
                <b> GiftSense gift finder</b> is switched on under App embeds, then click <b>Save</b>.
              </Text>
              <Text as="p" tone="subdued">You can change the button text, corner and colors there too.</Text>
            </Step>

            <Step
              number="2"
              title={<InlineStack gap="200" blockAlign="center"><span>Add a gift finder section</span><Badge>Optional</Badge></InlineStack>}
              action={<Button onClick={() => openEditor({ template: 'index', addAppBlockId: `${apiKey}/${BLOCK_HANDLE}`, target: 'newAppsSection' })}>
                Add to home page
              </Button>}
            >
              <Text as="p">
                A <b>Not sure what to get?</b> section with a button that opens the gift finder. Great on your home page,
                gift collections and holiday pages. You can move it or add it to other pages in the theme editor.
              </Text>
            </Step>

            <Step number="3" title="Try it on your store">
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
