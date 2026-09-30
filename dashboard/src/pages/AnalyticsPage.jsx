import { useState, useEffect } from 'react'
import { Page, Layout, Card, BlockStack, InlineStack, InlineGrid, Text, Banner, SkeletonBodyText, Box } from '@shopify/polaris'
import { fetchJson } from '../utils/shopifyFetch'

const pct = v => (v == null ? '—' : `${Math.round(v * 100)}%`)

function money(amount, currency) {
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency || 'USD', maximumFractionDigits: 0 }).format(amount)
  } catch {
    return `${currency} ${Math.round(amount)}`
  }
}

function Metric({ label, value, help }) {
  return (
    <Card>
      <BlockStack gap="100">
        <Text as="span" tone="subdued" variant="bodySm">{label}</Text>
        <Text as="p" variant="headingXl">{value}</Text>
        {help && <Text as="span" tone="subdued" variant="bodySm">{help}</Text>}
      </BlockStack>
    </Card>
  )
}

function DailyBars({ daily }) {
  const max = Math.max(1, ...daily.map(d => d.gift_orders))
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height: 120 }} aria-label="Gift orders per day, last 30 days">
      {daily.map(d => (
        <div key={d.date} title={`${d.date}: ${d.gift_orders} gift order${d.gift_orders === 1 ? '' : 's'}`}
          style={{ flex: 1, height: `${Math.max(2, (100 * d.gift_orders) / max)}%`, borderRadius: 3,
            background: d.gift_orders ? 'var(--p-color-bg-fill-brand)' : 'var(--p-color-bg-fill-secondary)' }} />
      ))}
    </div>
  )
}

function TopList({ title, items, empty }) {
  const total = items.reduce((n, i) => n + i.count, 0)
  return (
    <Card>
      <BlockStack gap="300">
        <Text as="h2" variant="headingMd">{title}</Text>
        {items.length === 0 ? <Text as="p" tone="subdued">{empty}</Text> : items.map(i => (
          <BlockStack key={i.value} gap="100">
            <InlineStack align="space-between"><Text as="span">{i.label}</Text><Text as="span" tone="subdued">{i.count}</Text></InlineStack>
            <div style={{ height: 6, borderRadius: 3, background: 'var(--p-color-bg-fill-secondary)' }}>
              <div style={{ height: 6, borderRadius: 3, width: `${(100 * i.count) / total}%`, background: 'var(--p-color-bg-fill-brand)' }} />
            </div>
          </BlockStack>
        ))}
      </BlockStack>
    </Card>
  )
}

export default function AnalyticsPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => { fetchJson('/api/analytics').then(setData).catch(() => setError('Could not load analytics. Please refresh.')) }, [])

  if (!data) {
    return (
      <Page title="Analytics">
        {error ? <Banner tone="critical" title={error} /> : <Card><SkeletonBodyText lines={8} /></Card>}
      </Page>
    )
  }

  const giftShare = data.all_orders ? `${Math.round((100 * data.gift_orders) / data.all_orders)}% of all ${data.all_orders} orders` : null
  const empty = data.sessions === 0 && data.gift_orders === 0

  return (
    <Page title="Analytics" subtitle={`Last ${data.days} days`}>
      <Layout>
        {empty && (
          <Layout.Section>
            <Banner tone="info" title="No gift finder activity yet">
              <p>Numbers appear here once shoppers use the gift finder on your store.</p>
            </Banner>
          </Layout.Section>
        )}
        <Layout.Section>
          <BlockStack gap="400">
            <InlineGrid columns={{ xs: 2, md: 4 }} gap="400">
              <Metric label="Gift finder sessions" value={data.sessions} help={`${data.searched_sessions} got gift ideas`} />
              <Metric label="Completion rate" value={pct(data.completion_rate)} help="Sessions that got gift ideas" />
              <Metric label="Gift orders" value={data.gift_orders} help={giftShare} />
              <Metric label="Revenue from gift finder" value={money(data.attributed_revenue, data.currency)}
                help={`${data.attributed_orders} order${data.attributed_orders === 1 ? '' : 's'} from a gift finder session`} />
            </InlineGrid>
            <InlineGrid columns={{ xs: 2, md: 4 }} gap="400">
              <Metric label="Search to order" value={pct(data.conversion_rate)} help="Sessions with gift ideas that ordered" />
              <Metric label="Gift item revenue" value={money(data.gift_revenue, data.currency)} help="Items marked as gifts" />
              <Metric label="Gift orders with a note" value={pct(data.note_attach_rate)} />
              <Metric label="Notes written with AI" value={pct(data.note_acceptance_rate)} help="Used our draft as-is or edited it" />
            </InlineGrid>
            <Card>
              <BlockStack gap="300">
                <Text as="h2" variant="headingMd">Gift orders per day</Text>
                <DailyBars daily={data.daily} />
                <InlineStack align="space-between">
                  <Text as="span" tone="subdued" variant="bodySm">{data.daily[0]?.date}</Text>
                  <Text as="span" tone="subdued" variant="bodySm">Today</Text>
                </InlineStack>
              </BlockStack>
            </Card>
            <InlineGrid columns={{ xs: 1, md: 2 }} gap="400">
              <TopList title="Top occasions" items={data.top_occasions} empty="No searches yet." />
              <TopList title="Top budgets" items={data.top_budgets} empty="No searches yet." />
            </InlineGrid>
            <Box paddingBlockEnd="400" />
          </BlockStack>
        </Layout.Section>
      </Layout>
    </Page>
  )
}
