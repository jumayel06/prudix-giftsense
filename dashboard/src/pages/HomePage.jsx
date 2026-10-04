import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Page, Card, BlockStack, InlineStack, InlineGrid, Text, ProgressBar, Button, Banner, Badge, Icon,
  SkeletonBodyText, Box, Link,
} from '@shopify/polaris'
import {
  NoteIcon, PackageIcon, CalendarIcon, MicrophoneIcon, ListBulletedIcon, PrintIcon, WandIcon, StoreIcon,
  ProductIcon, LightbulbIcon, ChatIcon, ArrowRightIcon,
} from '@shopify/polaris-icons'
import OnboardingChecklist from '../components/OnboardingChecklist'
import { fetchJson, shopifyFetch } from '../utils/shopifyFetch'

// Home: who's buying gifts and what's switched on, at a glance. Plan and
// usage come from /api/stats (App), everything else from /api/home.

function money(amount, currency) {
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency || 'USD', maximumFractionDigits: 0 }).format(amount)
  } catch {
    return `${currency} ${Math.round(amount)}`
  }
}

function greeting() {
  const h = new Date().getHours()
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'
}

const short = iso => new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })

function Change({ value, points }) {
  if (value == null) return <span className="gs-home-change gs-home-change--flat">new</span>
  const n = Math.round(value * 100)
  if (n === 0) return <span className="gs-home-change gs-home-change--flat">no change</span>
  return <span className={`gs-home-change ${n > 0 ? 'gs-home-change--up' : 'gs-home-change--down'}`}>{`${n > 0 ? '▲' : '▼'} ${Math.abs(n)}${points ? ' pts' : '%'}`}</span>
}

function Hero({ stats, home, navigate }) {
  const s = home?.summary
  const live = home?.storefront?.embed === 'on'
  const isTrial = stats.plan_status === 'trial_active'
  return (
    <div className="gs-home-hero">
      <div className="gs-home-hero__top">
        <div>
          <p className="gs-home-hero__eyebrow">{`${greeting()}${home ? `, ${home.store}` : ''}`}</p>
          <h1 className="gs-home-hero__title">Your gifting, at a glance</h1>
          <div className="gs-home-hero__chips">
            <span className="gs-home-chip">{`${stats.plan_name} plan`}</span>
            {isTrial && <span className="gs-home-chip gs-home-chip--accent">{`Trial · ${stats.trial_days_remaining} day${stats.trial_days_remaining === 1 ? '' : 's'} left`}</span>}
            {home && (
              <span className={`gs-home-chip ${live ? 'gs-home-chip--live' : 'gs-home-chip--off'}`}>
                <span className="gs-home-dot" />{live ? 'Gift finder is live' : 'Gift finder is off'}
              </span>
            )}
          </div>
        </div>
        <div className="gs-home-hero__actions">
          <Button onClick={() => navigate('/playground')}>Try the gift finder</Button>
          <Button variant="primary" onClick={() => navigate('/analytics')}>See analytics</Button>
        </div>
      </div>
      <div className="gs-home-hero__stats">
        {[
          ['Gift orders', s ? s.gift_orders.toLocaleString() : '—', s && <Change value={s.changes.gift_orders} />,
            s?.all_orders ? `${Math.round((100 * s.gift_orders) / s.all_orders)}% of all orders` : 'last 30 days'],
          ['Revenue from the gift finder', s ? money(s.attributed_revenue, s.currency) : '—', s && <Change value={s.changes.attributed_revenue} />, 'last 30 days'],
          ['Gift finder sessions', s ? s.sessions.toLocaleString() : '—', s && <Change value={s.changes.sessions} />, 'last 30 days'],
          ['Search to order', s?.conversion_rate != null ? `${Math.round(s.conversion_rate * 100)}%` : '—',
            s && s.conversion_rate != null && <Change value={s.changes.conversion_rate} />, 'sessions with ideas that ordered'],
        ].map(([label, value, change, help]) => (
          <div key={label} className="gs-home-stat">
            <span className="gs-home-stat__label">{label}</span>
            <span className="gs-home-stat__value">{value}{change}</span>
            <span className="gs-home-stat__help">{help}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function StatusCard({ icon, title, children, action }) {
  return (
    <Card>
      <BlockStack gap="300">
        <InlineStack gap="200" blockAlign="center">
          <span className="gs-home-icon"><Icon source={icon} /></span>
          <Text as="h2" variant="headingSm">{title}</Text>
        </InlineStack>
        {children}
        {action}
      </BlockStack>
    </Card>
  )
}

function UsageCard({ stats, navigate }) {
  const isTrial = stats.plan_status === 'trial_active'
  const limit = isTrial ? stats.trial_generations_cap : stats.generation_limit
  const used = isTrial ? stats.trial_generations_used : stats.generations_used
  const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0
  return (
    <StatusCard icon={WandIcon} title="AI generations"
      action={<InlineStack><Button variant="plain" onClick={() => navigate('/settings')}>AI settings</Button></InlineStack>}>
      <Text as="p" variant="headingLg">{`${used.toLocaleString()} `}<Text as="span" tone="subdued" variant="bodyMd">{`of ${limit.toLocaleString()}`}</Text></Text>
      <ProgressBar progress={pct} tone={pct >= 100 ? 'critical' : pct >= 75 ? 'highlight' : 'primary'} size="small" />
      <Text as="p" tone="subdued" variant="bodySm">
        {isTrial ? 'Used in your trial' : `${stats.days_remaining} days left this cycle`}
        {` · ${stats.ai_tier_label}${stats.ai_model_label ? ` (${stats.ai_model_label})` : ''}`}
      </Text>
    </StatusCard>
  )
}

function CatalogCard({ catalog, navigate }) {
  const pct = catalog.products ? Math.round((100 * catalog.analyzed) / catalog.products) : 0
  return (
    <StatusCard icon={ProductIcon} title="Catalog"
      action={<InlineStack><Button variant="plain" onClick={() => navigate('/catalog')}>View catalog</Button></InlineStack>}>
      <Text as="p" variant="headingLg">{`${catalog.analyzed.toLocaleString()} `}<Text as="span" tone="subdued" variant="bodyMd">{`of ${catalog.products.toLocaleString()} read by AI`}</Text></Text>
      <ProgressBar progress={pct} size="small" tone="success" />
      <Text as="p" tone="subdued" variant="bodySm">
        {catalog.sync_status === 'running' || catalog.sync_status === 'queued' ? 'Sync in progress…'
          : catalog.synced_at ? `Last synced ${short(catalog.synced_at)}` : 'Not synced yet'}
        {catalog.excluded ? ` · ${catalog.excluded} excluded` : ''}
      </Text>
    </StatusCard>
  )
}

function StorefrontCard({ storefront, navigate }) {
  const on = storefront.embed === 'on'
  return (
    <StatusCard icon={StoreIcon} title="Storefront"
      action={<InlineStack><Button variant="plain" onClick={() => navigate('/storefront')}>{on ? 'Storefront setup' : 'Turn it on'}</Button></InlineStack>}>
      <InlineStack gap="200" blockAlign="center">
        <Badge tone={on ? 'success' : storefront.embed === 'unknown' ? undefined : 'attention'}>
          {on ? 'Live' : storefront.embed === 'unknown' ? 'Not checked yet' : 'Off'}
        </Badge>
        {storefront.theme_name && <Text as="span" tone="subdued">{storefront.theme_name}</Text>}
      </InlineStack>
      <Text as="p" tone="subdued" variant="bodySm">
        {on ? 'Shoppers see the Find a gift button on your live theme.' : 'Shoppers can’t find gifts until the button is switched on in your theme.'}
      </Text>
    </StatusCard>
  )
}

function OrdersChart({ daily }) {
  const max = Math.max(1, ...daily.map(d => d.gift_orders))
  const total = daily.reduce((n, d) => n + d.gift_orders, 0)
  return (
    <Card>
      <BlockStack gap="300">
        <InlineStack align="space-between" blockAlign="baseline">
          <Text as="h2" variant="headingSm">Gift orders, last 14 days</Text>
          <Text as="span" tone="subdued" variant="bodySm">{`${total} total`}</Text>
        </InlineStack>
        <div className="gs-home-bars" aria-label="Gift orders per day, last 14 days">
          {daily.map(d => (
            <div key={d.date} className="gs-home-bar" title={`${short(d.date)}: ${d.gift_orders}`}>
              <div className={d.gift_orders ? 'gs-home-bar__fill' : 'gs-home-bar__fill gs-home-bar__fill--zero'}
                style={{ height: `${Math.max(4, (100 * d.gift_orders) / max)}%` }} />
            </div>
          ))}
        </div>
        <InlineStack align="space-between">
          <Text as="span" tone="subdued" variant="bodySm">{daily[0] && short(daily[0].date)}</Text>
          <Text as="span" tone="subdued" variant="bodySm">Today</Text>
        </InlineStack>
      </BlockStack>
    </Card>
  )
}

const TOOLKIT = [
  { key: 'notes', icon: NoteIcon, title: 'AI gift notes', text: 'Shoppers get a ready-to-print note in your tone.', to: '/notes' },
  { key: 'wrap', icon: PackageIcon, title: 'Gift wrap', text: 'Paid or free wrap, offered right in the gift panel.', to: '/wrap' },
  { key: 'arrive_by', icon: CalendarIcon, title: 'Arrive-by dates', text: 'Orders wait until it’s time to ship, so gifts land on the day.', to: '/delivery', plan: 'Growth' },
  { key: 'messages', icon: MicrophoneIcon, title: 'Voice & video', text: 'A recorded message behind a QR code on the gift card.', to: '/messages', plan: 'Growth' },
  { key: 'registries', icon: ListBulletedIcon, title: 'Registries', text: 'Shared wish lists that bring new shoppers to your store.', to: '/registries', plan: 'Pro' },
  { key: 'printing', icon: PrintIcon, title: 'Gift cards & receipts', text: 'Print notes and price-free receipts from any order.', to: '/orders' },
]

function Toolkit({ features, navigate }) {
  return (
    <BlockStack gap="300">
      <Text as="h2" variant="headingMd">Your gifting toolkit</Text>
      <div className="gs-home-toolkit">
        {TOOLKIT.map(t => {
          const f = features[t.key]
          const badge = !f.available ? <Badge tone="info">{t.plan}</Badge>
            : f.on ? <Badge tone="success">On</Badge> : <Badge>Off</Badge>
          return (
            <button type="button" key={t.key} className="gs-home-tool" onClick={() => navigate(t.to)}>
              <InlineStack align="space-between" blockAlign="start">
                <span className="gs-home-icon gs-home-icon--lg"><Icon source={t.icon} /></span>
                {badge}
              </InlineStack>
              <span className="gs-home-tool__title">{t.title}</span>
              <span className="gs-home-tool__text">{f.available && f.detail ? f.detail : t.text}</span>
              <span className="gs-home-tool__go">{!f.available ? 'See plans' : f.on ? 'Manage' : 'Set up'} <Icon source={ArrowRightIcon} /></span>
            </button>
          )
        })}
      </div>
    </BlockStack>
  )
}

function RecentOrders({ orders, navigate }) {
  return (
    <Card>
      <BlockStack gap="300">
        <InlineStack align="space-between" blockAlign="center">
          <Text as="h2" variant="headingSm">Latest gift orders</Text>
          <Button variant="plain" onClick={() => navigate('/orders')}>View all</Button>
        </InlineStack>
        {orders.length === 0 ? (
          <Text as="p" tone="subdued">Gift orders show up here as soon as shoppers check out with a gift.</Text>
        ) : orders.map(o => (
          <InlineStack key={o.order_id} align="space-between" blockAlign="center" wrap={false}>
            <BlockStack gap="050">
              <InlineStack gap="200" blockAlign="center">
                <Link url={`shopify://admin/orders/${o.order_id}`} removeUnderline>{o.order_name}</Link>
                {o.from_finder && <Badge tone="info" size="small">Gift finder</Badge>}
              </InlineStack>
              <Text as="span" tone="subdued" variant="bodySm">
                {[o.created_at && short(o.created_at), o.recipients.length ? `for ${o.recipients.join(', ')}` : null,
                  o.has_wrap ? 'wrapped' : null, o.has_message ? 'with a message' : null].filter(Boolean).join(' · ')}
              </Text>
            </BlockStack>
            <Text as="span" fontWeight="semibold">{money(o.gift_revenue, o.currency)}</Text>
          </InlineStack>
        ))}
      </BlockStack>
    </Card>
  )
}

function Tips({ home, navigate }) {
  const tips = []
  if (home.storefront.embed !== 'on') tips.push(['Switch on the Find a gift button so shoppers can use it.', 'Storefront setup', '/storefront'])
  if (home.catalog.products && home.catalog.analyzed < home.catalog.products) tips.push(['Some products haven’t been read by the AI yet; they’re added automatically.', 'View catalog', '/catalog'])
  if (home.features.wrap.available && !home.features.wrap.on) tips.push(['Shops that offer wrap often see it added to a share of gift orders. It takes a minute to set up.', 'Set up gift wrap', '/wrap'])
  if (home.features.arrive_by.available && !home.features.arrive_by.on) tips.push(['Let shoppers pick an arrival date so birthday gifts don’t land a week early.', 'Arrive-by dates', '/delivery'])
  tips.push(['Add the Find a gift block to your home page or a gift collection, not just the floating button.', 'Storefront setup', '/storefront'])
  const [text, cta, to] = tips[0]
  return (
    <Card>
      <BlockStack gap="300">
        <InlineStack gap="200" blockAlign="center">
          <span className="gs-home-icon gs-home-icon--accent"><Icon source={LightbulbIcon} /></span>
          <Text as="h2" variant="headingSm">Tip</Text>
        </InlineStack>
        <Text as="p">{text}</Text>
        <InlineStack><Button onClick={() => navigate(to)}>{cta}</Button></InlineStack>
        <Box borderBlockStartWidth="025" borderColor="border-secondary" paddingBlockStart="300">
          <InlineStack gap="200" blockAlign="center">
            <span className="gs-home-icon"><Icon source={ChatIcon} /></span>
            <Text as="span" tone="subdued">Questions? <Link onClick={() => navigate('/support')}>Contact support</Link></Text>
          </InlineStack>
        </Box>
      </BlockStack>
    </Card>
  )
}

export default function HomePage({ stats }) {
  const navigate = useNavigate()
  const [home, setHome] = useState(null)
  const [error, setError] = useState(false)

  useEffect(() => { fetchJson('/api/home').then(setHome).catch(() => setError(true)) }, [])

  // The review banner shows once per shop: tell the backend once it's on screen
  // (/api/stats only reports eligibility; other screens call it too).
  const showReview = Boolean(stats?.show_review_prompt && stats?.review_prompt_url)
  const reportedSeen = useRef(false)
  useEffect(() => {
    if (!showReview || reportedSeen.current) return
    reportedSeen.current = true
    shopifyFetch('/api/review-prompt/seen', { method: 'POST' }).catch(() => {})
  }, [showReview])

  if (!stats) {
    return (
      <Page title="Prudix GiftSense">
        <Banner tone="critical" title="Could not load your account. Please refresh the page." />
      </Page>
    )
  }

  return (
    <Page>
      <BlockStack gap="500">
        {stats.plan_status === 'cancelled' && stats.access_until && (
          <Banner tone="warning" title={`Your plan is cancelled. Access continues until ${new Date(stats.access_until).toLocaleDateString()}.`}>
            <Button onClick={() => navigate('/plans')}>Choose a plan</Button>
          </Banner>
        )}
        {stats.scheduled_plan_name && stats.scheduled_change_at && (
          <Banner tone="info" title={`Your plan changes to ${stats.scheduled_plan_name} on ${new Date(stats.scheduled_change_at).toLocaleDateString()}.`} />
        )}
        {showReview && (
          <Banner tone="success" title="Enjoying GiftSense?">
            <Button url={stats.review_prompt_url} target="_blank">Leave a review</Button>
          </Banner>
        )}
        {error && <Banner tone="warning" title="Some of your numbers didn’t load. Refresh to try again." />}

        <Hero stats={stats} home={home} navigate={navigate} />

        {['active', 'trial_active'].includes(stats.plan_status) && <OnboardingChecklist />}

        <InlineGrid columns={{ xs: 1, md: 3 }} gap="400">
          <UsageCard stats={stats} navigate={navigate} />
          {home ? <CatalogCard catalog={home.catalog} navigate={navigate} /> : <Card><SkeletonBodyText lines={4} /></Card>}
          {home ? <StorefrontCard storefront={home.storefront} navigate={navigate} /> : <Card><SkeletonBodyText lines={4} /></Card>}
        </InlineGrid>

        {home ? (
          <>
            <div className="gs-home-split">
              <OrdersChart daily={home.summary.daily} />
              <Tips home={home} navigate={navigate} />
            </div>
            <RecentOrders orders={home.recent_orders} navigate={navigate} />
            <Toolkit features={home.features} navigate={navigate} />
          </>
        ) : !error && <Card><SkeletonBodyText lines={8} /></Card>}
      </BlockStack>
    </Page>
  )
}
