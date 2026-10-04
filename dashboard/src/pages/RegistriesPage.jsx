import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Page, Layout, Card, BlockStack, InlineGrid, InlineStack, Text, Button, Banner, SkeletonBodyText, IndexTable,
  EmptyState,
} from '@shopify/polaris'
import { fetchJson } from '../utils/shopifyFetch'

const day = iso => {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number)
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

function money(amount, currency) {
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(amount)
  } catch {
    return `${currency} ${Number(amount).toFixed(2)}`
  }
}

function Stat({ label, value, help }) {
  return (
    <Card>
      <BlockStack gap="100">
        <Text as="p" tone="subdued">{label}</Text>
        <Text as="p" variant="headingLg">{value}</Text>
        {help && <Text as="p" tone="subdued" variant="bodySm">{help}</Text>}
      </BlockStack>
    </Card>
  )
}

export default function RegistriesPage() {
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchJson('/api/registries').then(setData).catch(() => setError('Could not load registries. Please refresh.'))
  }, [])

  if (!data) {
    return (
      <Page title="Registries">
        {error ? <Banner tone="critical" title={error} /> : <Card><SkeletonBodyText lines={6} /></Card>}
      </Page>
    )
  }

  const subtitle = 'Customers build a wish list from your store and share it with friends and family.'
  if (!data.available && data.totals.registries === 0) {
    return (
      <Page title="Registries" subtitle={subtitle}>
        <Card>
          <BlockStack gap="300">
            <Text as="p">Logged-in customers add products to a registry from your product pages and share the link. Guests see what&apos;s still wanted and buy it here, so every shared list brings new shoppers to your store. Customers also get AI gift ideas from your catalog.</Text>
            <Text as="p" tone="subdued">Available on the Pro plan.</Text>
            <InlineStack><Button variant="primary" onClick={() => navigate('/plans')}>See plans</Button></InlineStack>
          </BlockStack>
        </Card>
      </Page>
    )
  }

  const t = data.totals
  return (
    <Page title="Registries" subtitle={subtitle}>
      <Layout>
        <Layout.Section>
          <BlockStack gap="300">
            {!data.available && (
              <Banner tone="warning" title="Registries are off on your current plan">
                <p>Shared registry pages show as unavailable until you&apos;re back on Pro.</p>
              </Banner>
            )}
            <InlineGrid columns={{ xs: 2, md: 4 }} gap="300">
              <Stat label="Registries" value={t.registries.toLocaleString()} help={`${t.items.toLocaleString()} products on them`} />
              <Stat label="Guest visits" value={t.views.toLocaleString()} help="Opens of shared registry pages" />
              <Stat label="Gifts bought" value={`${t.bought.toLocaleString()} of ${t.wanted.toLocaleString()}`} help="Items wanted across all registries" />
              <Stat label="Sales from registries" value={money(t.revenue, t.currency)} help={`${t.orders.toLocaleString()} orders`} />
            </InlineGrid>
          </BlockStack>
        </Layout.Section>
        <Layout.Section>
          <Card padding="0">
            {data.registries.length === 0 ? (
              <EmptyState heading="No registries yet" image="">
                <p>Add the Gift options block to your product template (Storefront page). Logged-in customers then see Add to registry next to This is a gift.</p>
              </EmptyState>
            ) : (
              <IndexTable
                resourceName={{ singular: 'registry', plural: 'registries' }}
                itemCount={data.registries.length}
                selectable={false}
                headings={[{ title: 'Registry' }, { title: 'Occasion' }, { title: 'Event date' }, { title: 'Products' }, { title: 'Bought' }, { title: 'Guest visits' }, { title: 'Created' }]}
              >
                {data.registries.map((r, i) => (
                  <IndexTable.Row id={String(i)} key={i} position={i}>
                    <IndexTable.Cell><Text as="span" fontWeight="semibold">{r.title}</Text></IndexTable.Cell>
                    <IndexTable.Cell>{r.occasion}</IndexTable.Cell>
                    <IndexTable.Cell>{r.event_date ? day(r.event_date) : '—'}</IndexTable.Cell>
                    <IndexTable.Cell>{r.items}</IndexTable.Cell>
                    <IndexTable.Cell>{`${r.bought} of ${r.wanted}`}</IndexTable.Cell>
                    <IndexTable.Cell>{r.views}</IndexTable.Cell>
                    <IndexTable.Cell>{r.created_at ? day(r.created_at) : '—'}</IndexTable.Cell>
                  </IndexTable.Row>
                ))}
              </IndexTable>
            )}
          </Card>
        </Layout.Section>
        <Layout.Section>
          <Text as="p" tone="subdued" variant="bodySm">
            GiftSense keeps no names, emails or addresses for registries, only Shopify&apos;s customer id. Guests enter the
            delivery address in your normal checkout. Customers manage their registry at /apps/giftsense/registry on your store.
          </Text>
        </Layout.Section>
      </Layout>
    </Page>
  )
}
