import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Page, Layout, Card, BlockStack, InlineStack, InlineGrid, Text, Banner, SkeletonBodyText, Box, ButtonGroup, Button,
  Select, Pagination, Badge, IndexTable, EmptyState, Tooltip, Icon,
} from '@shopify/polaris'
import { LockFilledIcon, InfoIcon } from '@shopify/polaris-icons'
import { fetchJson } from '../utils/shopifyFetch'
import { prefetchPlans } from '../utils/bootData'

const pct = v => (v == null ? '—' : `${Math.round(v * 100)}%`)

function money(amount, currency) {
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency || 'USD', maximumFractionDigits: 0 }).format(amount)
  } catch {
    return `${currency} ${Math.round(amount)}`
  }
}

// "Oct 2 – Oct 8" for the `days` days ending `daysAgo` days before today.
function span(days, daysAgo = 0) {
  const fmt = d => d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  const end = new Date()
  end.setDate(end.getDate() - daysAgo)
  const start = new Date(end)
  start.setDate(start.getDate() - (days - 1))
  return `${fmt(start)} – ${fmt(end)}`
}

// Change from the previous period (full analytics only), as a small badge next
// to the number, like Shopify's own analytics: "↑ 25%", or "New" when the
// previous period was zero. Hover shows the previous value and its dates.
function Change({ now, before, format, period }) {
  if (now == null || before == null || now === before) return null
  const up = now > before
  const label = before ? `${up ? '↑' : '↓'} ${Math.abs(Math.round(((now - before) / before) * 100))}%` : 'New'
  return (
    <Tooltip content={`Was ${format(before)} in ${period}`}>
      <Badge tone={up ? 'success' : 'critical'} size="small">{label}</Badge>
    </Tooltip>
  )
}

// "🔒 Upgrade to unlock" pill, centred over a locked card's faded placeholder.
// What each card means, in plain words: the ⓘ hover on every card, and the
// hover on locked cards (followed by which plans include it).
const INFO = {
  'Gift finder sessions': 'How many times shoppers opened the gift finder on your store.',
  'Completion rate': 'Of the shoppers who opened the gift finder, the share who answered the questions and got gift ideas.',
  'Gift orders': 'Orders that used any GiftSense gift option, like a gift note, gift wrap or marking an item as a gift.',
  'Sales from the gift finder': 'The full total of orders where the shopper added something to their cart from the gift finder, including anything else in that cart.',
  'Search to order': 'Of the shoppers who got gift ideas, the share who then placed an order.',
  'Sales of gifts': "Just the products shoppers marked as gifts, in any order. Other items in the same cart aren't counted. Example: a $40 gift plus $20 for themselves counts $40.",
  'Gift orders with a note': 'The share of gift orders that included a gift note.',
  'Notes written with AI': 'Of the gift orders with a note, the share where the shopper used the AI-written note, as it was or after editing it.',
  'Gift orders per day': 'How many gift orders came in each day. Hover a bar to see the date and count.',
  'Top occasions': 'What shoppers told the gift finder they were shopping for, like a birthday or anniversary.',
  'Top budgets': 'The price ranges shoppers picked in the gift finder.',
  'Top recipients': 'Who shoppers told the gift finder the gift was for, like a parent or a partner.',
  'Gift options on gift orders': 'How often gift orders included each extra: gift wrap, a voice or video message, an arrive-by date or a gift note.',
  'How gifts are sent': 'Whether gifts were shipped straight to the person receiving them or handed over by the shopper, plus orders from gift registries.',
  'Generation history': "Each time the AI did work for your store, like suggesting gifts or writing a note. These count toward your plan's monthly AI limit.",
}

// ⓘ next to a card title; hover shows INFO[title].
function InfoTip({ title }) {
  if (!INFO[title]) return null
  return (
    <Tooltip content={INFO[title]} width="wide">
      <span style={{ display: 'flex', cursor: 'help' }}><Icon source={InfoIcon} tone="subdued" /></span>
    </Tooltip>
  )
}

// Card heading with its ⓘ.
function Title({ children }) {
  return (
    <InlineStack gap="100" blockAlign="center" wrap={false}>
      <Text as="h2" variant="headingMd">{children}</Text>
      <InfoTip title={children} />
    </InlineStack>
  )
}

function LockPill() {
  return (
    <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 12px', borderRadius: 999,
        background: 'var(--p-color-bg-surface)', border: '1px solid var(--p-color-border)', boxShadow: 'var(--p-shadow-200)' }}>
        <span><Icon source={LockFilledIcon} /></span>
        <Text as="span" variant="bodySm" fontWeight="semibold">Upgrade to unlock</Text>
      </div>
    </div>
  )
}

function Metric({ label, value, help, change, locked }) {
  if (locked) {
    return (
      <Tooltip content={`${INFO[label] || ''} ${locked.hint}.`.trim()} preferredPosition="above" width="wide" activatorWrapper="div">
        <div onClick={locked.onClick} style={{ cursor: 'pointer', height: '100%' }}>
          <Card>
            <BlockStack gap="100">
              <InlineStack align="space-between" blockAlign="center" wrap={false}>
                <Text as="span" tone="subdued" variant="bodySm">{label}</Text>
                <span><Icon source={LockFilledIcon} tone="subdued" /></span>
              </InlineStack>
              <div style={{ position: 'relative' }}>
                <div style={{ opacity: 0.35 }}><SkeletonBodyText lines={2} /></div>
                <LockPill />
              </div>
            </BlockStack>
          </Card>
        </div>
      </Tooltip>
    )
  }
  return (
    <Card>
      <BlockStack gap="100">
        <InlineStack gap="100" blockAlign="center" wrap={false}>
          <Text as="span" tone="subdued" variant="bodySm">{label}</Text>
          <InfoTip title={label} />
        </InlineStack>
        <InlineStack gap="200" blockAlign="center">
          <Text as="p" variant="headingXl">{value}</Text>
          {change}
        </InlineStack>
        {help && <Text as="span" tone="subdued" variant="bodySm">{help}</Text>}
      </BlockStack>
    </Card>
  )
}

function Rows({ title, rows }) {
  return (
    <Card>
      <BlockStack gap="300">
        <Title>{title}</Title>
        {rows.map(([label, value]) => (
          <InlineStack key={label} align="space-between"><Text as="span">{label}</Text><Text as="span" fontWeight="semibold">{value}</Text></InlineStack>
        ))}
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
        <Title>{title}</Title>
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

// "Growth and Pro": the plans that include `feature`, from /api/plans.
function planNames(plans, feature) {
  const names = (plans || []).filter(p => p.features.includes(feature)).map(p => p.name)
  return names.length > 1 ? `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}` : names[0] || 'a higher plan'
}

// A full-analytics card on a plan without it: the title, a faded placeholder
// (no data) and a lock pill over it.
function LockedCard({ title, lines = 4, hint, onClick }) {
  return (
    <Tooltip content={`${INFO[title] || ''} ${hint}.`.trim()} preferredPosition="above" width="wide" activatorWrapper="div">
      <div onClick={onClick} style={{ cursor: 'pointer', height: '100%' }}>
        <Card>
          <BlockStack gap="300">
            <InlineStack align="space-between" blockAlign="center">
              <Text as="h2" variant="headingMd" tone="subdued">{title}</Text>
              <span><Icon source={LockFilledIcon} tone="subdued" /></span>
            </InlineStack>
            <div style={{ position: 'relative' }}>
              <div style={{ opacity: 0.35 }}><SkeletonBodyText lines={lines} /></div>
              <LockPill />
            </div>
          </BlockStack>
        </Card>
      </div>
    </Tooltip>
  )
}

// Date-range presets for the generation log (independent of the funnel
// metrics' days selector above — this table is available on every plan).
const GEN_RANGE_PRESETS = [
  { label: 'Last 90 days', value: '90' },
  { label: 'Last 30 days', value: '30' },
  { label: 'Last 7 days',  value: '7' },
  { label: 'All time',     value: 'all' },
]

function genWhen(iso) {
  return iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '—'
}

// Paginated log of individual AI generations — feature, model, status —
// ported from Prudix Commerce's Analytics page (/api/analytics/generations).
function GenerationsTable() {
  const PAGE_SIZE = 25
  const [rows, setRows] = useState(null)
  const [page, setPage] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [range, setRange] = useState('90')
  const [feature, setFeature] = useState('')
  const [model, setModel] = useState('')
  const [facets, setFacets] = useState({ features: [], models: [] })

  const fetchPage = p => {
    setLoading(true)
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(p * PAGE_SIZE) })
    if (range === 'all') {
      params.set('start', '1970-01-01')
    } else {
      const start = new Date()
      start.setDate(start.getDate() - Number(range))
      params.set('start', start.toISOString())
    }
    if (feature) params.set('feature', feature)
    if (model) params.set('model', model)
    fetchJson(`/api/analytics/generations?${params.toString()}`)
      .then(d => {
        setRows(d.generations || [])
        setHasMore(Boolean(d.has_more))
        setTotal(d.total || 0)
        setPage(p)
        if (d.facets) setFacets(d.facets)
      })
      .catch(() => setRows([]))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    fetchPage(0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range, feature, model])

  const filtered = Boolean(feature || model || range !== '90')
  const featureOptions = [{ label: 'All features', value: '' }, ...facets.features.map(f => ({ label: f, value: f }))]
  const modelOptions = [{ label: 'All models', value: '' }, ...facets.models.map(m => ({ label: m.label, value: m.value }))]

  return (
    <Card padding="0">
      <BlockStack gap="0">
        <Box padding="400" paddingBlockEnd="300">
          <BlockStack gap="300">
            <BlockStack gap="100">
              <Title>Generation history</Title>
              <Text as="p" tone="subdued" variant="bodySm">Every generation, newest first — the feature it powered, the model, and status.</Text>
            </BlockStack>
            <InlineStack gap="200">
              <div style={{ minWidth: 150 }}><Select label="Date range" labelHidden options={GEN_RANGE_PRESETS} value={range} onChange={setRange} /></div>
              <div style={{ minWidth: 170 }}><Select label="Feature" labelHidden options={featureOptions} value={feature} onChange={setFeature} /></div>
              <div style={{ minWidth: 170 }}><Select label="Model" labelHidden options={modelOptions} value={model} onChange={setModel} /></div>
              {filtered && <Button variant="plain" onClick={() => { setFeature(''); setModel(''); setRange('90') }}>Reset</Button>}
            </InlineStack>
          </BlockStack>
        </Box>
        {rows === null ? (
          <Box padding="400"><SkeletonBodyText lines={4} /></Box>
        ) : rows.length === 0 ? (
          <EmptyState heading={filtered ? 'No generations match these filters' : 'No generations yet'} image="">
            <p>{filtered ? 'Try widening the date range or clearing a filter.' : "Once you generate content it'll show up here."}</p>
          </EmptyState>
        ) : (
          <>
            <IndexTable
              resourceName={{ singular: 'generation', plural: 'generations' }}
              itemCount={rows.length}
              selectable={false}
              headings={[{ title: 'When' }, { title: 'Feature' }, { title: 'Model' }, { title: 'Gens', alignment: 'end' }, { title: 'Status' }]}
            >
              {rows.map((r, i) => (
                <IndexTable.Row id={r.id} key={r.id} position={i}>
                  <IndexTable.Cell>{genWhen(r.when)}</IndexTable.Cell>
                  <IndexTable.Cell>{r.feature}</IndexTable.Cell>
                  <IndexTable.Cell>{r.model || '—'}</IndexTable.Cell>
                  <IndexTable.Cell><Text as="span" alignment="end" numeric>{r.gens}</Text></IndexTable.Cell>
                  <IndexTable.Cell><Badge tone={r.status === 'Completed' ? 'success' : 'subdued'} size="small">{r.status}</Badge></IndexTable.Cell>
                </IndexTable.Row>
              ))}
            </IndexTable>
            <Box padding="300">
              <InlineStack align="space-between" blockAlign="center">
                <Text as="span" tone="subdued" variant="bodySm">{`${page * PAGE_SIZE + 1}–${page * PAGE_SIZE + rows.length} of ${total}`}</Text>
                <Pagination
                  hasPrevious={page > 0 && !loading} onPrevious={() => fetchPage(page - 1)}
                  hasNext={hasMore && !loading} onNext={() => fetchPage(page + 1)}
                />
              </InlineStack>
            </Box>
          </>
        )}
      </BlockStack>
    </Card>
  )
}

export default function AnalyticsPage() {
  const navigate = useNavigate()
  const [days, setDays] = useState(30)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [plans, setPlans] = useState(null)

  useEffect(() => { prefetchPlans().then(d => setPlans(d ? d.plans : null)) }, [])

  useEffect(() => {
    fetchJson(`/api/analytics?days=${days}`).then(setData).catch(() => setError('Could not load analytics. Please refresh.'))
  }, [days])

  if (!data) {
    return (
      <Page title="Analytics">
        {error ? <Banner tone="critical" title={error} /> : <Card><SkeletonBodyText lines={8} /></Card>}
      </Page>
    )
  }

  const giftShare = data.full && data.all_orders ? `${Math.round((100 * data.gift_orders) / data.all_orders)}% of all orders` : null
  const empty = data.sessions === 0 && data.gift_orders === 0
  const ch = (key, format = String) => (data.full
    ? <Change now={data[key]} before={data.previous[key]} format={format} period={span(data.days, data.days)} />
    : null)
  const cash = v => money(v, data.currency)
  const mix = data.delivery_mix || { direct: 0, self: 0 }
  // Plans without full analytics see the same page with those parts locked:
  // 30 days only, and a hover hint naming the plans that include the rest.
  const hint = `Available on ${planNames(plans, 'full_analytics')}`
  const toPlans = () => navigate('/plans')
  const lock = data.full ? undefined : { hint, onClick: toPlans }
  const range = (
    <ButtonGroup variant="segmented">
      {[7, 30, 90].map(d => (data.full || d === 30
        ? <Button key={d} pressed={days === d} onClick={() => setDays(d)}>{`${d} days`}</Button>
        : <Tooltip key={d} content={hint}><Button icon={LockFilledIcon} onClick={toPlans}>{`${d} days`}</Button></Tooltip>))}
    </ButtonGroup>
  )

  return (
    <Page title="Analytics" subtitle={span(data.days)}
      secondaryActions={<div>{range}</div>}>
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
            <div className="gs-equal-cards"><InlineGrid columns={{ xs: 2, md: 4 }} gap="400">
              <Metric label="Gift finder sessions" value={data.sessions} change={ch('sessions')} />
              <Metric label="Completion rate" value={pct(data.completion_rate)} locked={lock} />
              <Metric label="Gift orders" value={data.gift_orders} help={giftShare} change={ch('gift_orders')} />
              <Metric label="Sales from the gift finder" value={money(data.attributed_revenue, data.currency)} change={ch('attributed_revenue', cash)}
                help={`${data.attributed_orders} order${data.attributed_orders === 1 ? '' : 's'}`} />
            </InlineGrid></div>
            <div className="gs-equal-cards"><InlineGrid columns={{ xs: 2, md: 4 }} gap="400">
              <Metric label="Search to order" value={pct(data.conversion_rate)} change={ch('conversion_rate', pct)} locked={lock} />
              <Metric label="Sales of gifts" value={money(data.gift_revenue, data.currency)} change={ch('gift_revenue', cash)} />
              <Metric label="Gift orders with a note" value={pct(data.note_attach_rate)} locked={lock} />
              <Metric label="Notes written with AI" value={pct(data.note_acceptance_rate)} locked={lock} />
            </InlineGrid></div>
            <Card>
              <BlockStack gap="300">
                <Title>Gift orders per day</Title>
                <DailyBars daily={data.daily} />
                <InlineStack align="space-between">
                  <Text as="span" tone="subdued" variant="bodySm">{data.daily[0]?.date}</Text>
                  <Text as="span" tone="subdued" variant="bodySm">Today</Text>
                </InlineStack>
              </BlockStack>
            </Card>
            <div className="gs-equal-cards"><InlineGrid columns={{ xs: 1, md: 3 }} gap="400">
              {[['Top occasions', data.top_occasions], ['Top budgets', data.top_budgets], ['Top recipients', data.top_recipients]].map(([title, items]) => (data.full
                ? <TopList key={title} title={title} items={items} empty="No searches yet." />
                : <LockedCard key={title} title={title} hint={hint} onClick={toPlans} />))}
            </InlineGrid></div>
            {data.full ? (
              <div className="gs-equal-cards"><InlineGrid columns={{ xs: 1, md: 2 }} gap="400">
                <Rows title="Gift options on gift orders" rows={[
                  ['Gift wrap', pct(data.wrap_attach_rate)],
                  ['Voice or video message', `${pct(data.message_attach_rate)} (${data.messages.voice} voice, ${data.messages.video} video)`],
                  ['Arrive-by date', pct(data.arrive_by_rate)],
                  ['Gift note', pct(data.note_attach_rate)],
                ]} />
                <Rows title="How gifts are sent" rows={[
                  ['Shipped straight to the recipient', mix.direct],
                  ['Given by the shopper', mix.self],
                  ['Gifts per order (given by the shopper)', data.gifts_per_order ?? '—'],
                  ['Registry orders', `${data.registry_orders} · ${money(data.registry_revenue, data.currency)}`],
                ]} />
              </InlineGrid></div>
            ) : (
              <div className="gs-equal-cards"><InlineGrid columns={{ xs: 1, md: 2 }} gap="400">
                <LockedCard title="Gift options on gift orders" hint={hint} onClick={toPlans} />
                <LockedCard title="How gifts are sent" hint={hint} onClick={toPlans} />
              </InlineGrid></div>
            )}
            <GenerationsTable />
            <Box paddingBlockEnd="400" />
          </BlockStack>
        </Layout.Section>
      </Layout>
    </Page>
  )
}
