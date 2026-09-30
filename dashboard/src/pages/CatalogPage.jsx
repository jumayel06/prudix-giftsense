import { useState, useEffect, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Page, Layout, Card, BlockStack, InlineStack, Text, Banner, ProgressBar, Badge,
  Thumbnail, TextField, Pagination, Button, SkeletonBodyText, EmptyState,
} from '@shopify/polaris'
import { ImageIcon } from '@shopify/polaris-icons'
import { shopifyFetch, fetchJson } from '../utils/shopifyFetch'
import { parseApiError } from '../utils/apiError'
import { showToast } from '../utils/toast'
import EditProfileModal from '../components/EditProfileModal'

const POLL_MS = 5000
const IN_PROGRESS = ['queued', 'running', 'importing']

function money(n) {
  return `$${Number(n).toFixed(2).replace(/\.00$/, '')}`
}

function timeOf(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function SyncCard({ status, onResync, resyncing }) {
  const sync = status.sync
  const busy = sync && IN_PROGRESS.includes(sync.status)
  // A re-sync only re-analyzes changed products, so progress is "products ready
  // for gift matching" (moves as each one is analyzed), not sync.enriched.
  const pct = sync?.status === 'importing' && status.products
    ? Math.max(5, Math.round((100 * status.analyzed) / status.products)) : null
  const slowStart = !!sync?.slow_start
  const coolingDown = !busy && !!status.next_manual_sync_at

  return (
    <Card>
      <BlockStack gap="300">
        <InlineStack align="space-between" blockAlign="center">
          <Text as="h2" variant="headingMd">Catalog analysis</Text>
          <BlockStack gap="050" inlineAlign="end">
            <Button onClick={onResync} loading={resyncing} disabled={busy || coolingDown}>Sync now</Button>
            {coolingDown && (
              <Text as="span" variant="bodySm" tone="subdued">Available again at {timeOf(status.next_manual_sync_at)}</Text>
            )}
          </BlockStack>
        </InlineStack>

        {busy && (
          <BlockStack gap="200">
            <Text as="p">
              {sync.status === 'queued' && 'Step 1 of 2: Waiting to start…'}
              {sync.status === 'running' && 'Step 1 of 2: Shopify is preparing your product list…'}
              {sync.status === 'importing' && `Step 2 of 2: Analyzing products… ${status.analyzed} of ${status.products} ready`}
            </Text>
            <ProgressBar progress={pct ?? 10} size="small" />
            {sync.status !== 'importing' && (
              <Text as="p" variant="bodySm" tone="subdued">
                Usually under a minute; large catalogs can take a few minutes. Only new or changed products are re-analyzed.
              </Text>
            )}
            {slowStart && (
              <Banner tone="warning">
                <p>This is taking longer than usual to start. It will run as soon as our background service picks it up; if it's still waiting in 30 minutes, contact support.</p>
              </Banner>
            )}
          </BlockStack>
        )}

        {!busy && sync?.status === 'failed' && (
          <Banner tone="warning" title="The last sync didn't finish">
            <p>We'll retry automatically tonight, or you can sync now.</p>
          </Banner>
        )}

        {!sync && <Text as="p" tone="subdued">Your first sync starts within a few minutes of choosing a plan.</Text>}

        <InlineStack gap="400">
          <Text as="p"><b>{status.analyzed}</b> of {status.products} products ready for gift matching</Text>
          {status.excluded > 0 && <Text as="p" tone="subdued">{status.excluded} excluded</Text>}
        </InlineStack>
        <Text as="p" variant="bodySm" tone="subdued">
          Edited products re-analyzed this month: {status.rereads_used} of {status.rereads_limit}.
          New products and price or stock changes don't count.
        </Text>

        {status.held > 0 && (
          <Banner tone="info">
            <p>
              {status.held} edited product{status.held === 1 ? '' : 's'} will be re-analyzed when your monthly
              allowance resets. Until then they keep working in the gift finder with their current profile.
            </p>
          </Banner>
        )}

        {status.products >= status.limit && (
          <Banner tone="info">
            <p>
              {status.is_trial
                ? `During the free trial we analyze up to ${status.limit} products. The rest are added when your plan starts.`
                : `Your plan covers up to ${status.limit} products. Upgrade to include more of your catalog.`}
            </p>
          </Banner>
        )}
      </BlockStack>
    </Card>
  )
}

const CLAMP_2 = { display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }
const MAX_CHIPS = 3

// One badge per row, most important first.
function statusBadge(p, hasProfile) {
  if (p.excluded) return <Badge>Excluded</Badge>
  if (!p.available) return <Badge tone="critical">Out of stock</Badge>
  if (!hasProfile) return <Badge tone="attention">Analyzing…</Badge>
  if (p.update_pending) return <Badge tone="info">Update pending</Badge>
  if (p.overridden) return <Badge tone="success">Edited by you</Badge>
  if (p.profile_fallback) return <Badge tone="warning">Basic profile</Badge>
  return null
}

function LabeledList({ label, values, labels }) {
  if (!values || values.length === 0) return null
  const shown = values.slice(0, MAX_CHIPS).map(v => labels[v] || v)
  const more = values.length - shown.length
  return (
    <Text as="p" variant="bodyMd">
      <Text as="span" tone="subdued">{label}: </Text>
      {shown.join(', ')}{more > 0 ? ` +${more} more` : ''}
    </Text>
  )
}

function ProductRow({ p, labels, toggling, onToggle, onEdit }) {
  const prof = p.profile || {}
  const hasProfile = p.analyzed || p.update_pending
  const price = p.price_min === p.price_max ? money(p.price_min) : `${money(p.price_min)}–${money(p.price_max)}`
  const showProfile = hasProfile && !p.excluded
  return (
    <div className="gs-product-row" style={{
      padding: 'var(--p-space-400)', borderTop: 'var(--p-border-width-025) solid var(--p-color-border-secondary)',
      display: 'flex', gap: 'var(--p-space-400)', alignItems: 'flex-start', flexWrap: 'wrap',
    }}>
      <div style={{ opacity: p.excluded ? 0.5 : 1 }}>
        <Thumbnail source={p.image_url || ImageIcon} alt={p.title} size="large" />
      </div>
      <div style={{ flex: '1 1 260px', minWidth: 0, opacity: p.excluded ? 0.6 : 1 }}>
        <BlockStack gap="200">
          <BlockStack gap="100">
            <InlineStack gap="200" blockAlign="center">
              <Text as="h3" variant="headingSm">{p.title}</Text>
              {statusBadge(p, hasProfile)}
            </InlineStack>
            <Text as="p" variant="bodySm" tone="subdued">{[price, p.product_type].filter(Boolean).join(' · ')}</Text>
          </BlockStack>
          {showProfile && prof.gift_pitch && (
            <div style={CLAMP_2}><Text as="p" variant="bodyMd">{prof.gift_pitch}</Text></div>
          )}
          {showProfile && (
            <BlockStack gap="050">
              <LabeledList label="Good for" values={prof.recipients} labels={labels} />
              <LabeledList label="Occasions" values={prof.occasions} labels={labels} />
            </BlockStack>
          )}
          {p.excluded && <Text as="p" variant="bodySm" tone="subdued">Never suggested by the gift finder.</Text>}
        </BlockStack>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--p-space-200)', alignItems: 'stretch', minWidth: 130 }}>
        {showProfile && <Button onClick={() => onEdit(p)}>Edit gift profile</Button>}
        <Button variant="tertiary" tone={p.excluded ? undefined : 'critical'} loading={toggling} onClick={() => onToggle(p)}>
          {p.excluded ? 'Include again' : 'Exclude'}
        </Button>
      </div>
    </div>
  )
}

export default function CatalogPage() {
  const navigate = useNavigate()
  const [status, setStatus] = useState(null)
  const [list, setList] = useState(null)
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [error, setError] = useState(null)
  const [resyncing, setResyncing] = useState(false)
  const [toggling, setToggling] = useState(null)
  const [editing, setEditing] = useState(null)
  const [options, setOptions] = useState(null)
  const searchTimer = useRef(null)

  const loadStatus = useCallback(() =>
    fetchJson('/api/catalog/status').then(setStatus).catch(() => setError('Could not load your catalog. Please refresh.')),
  [])

  const loadProducts = useCallback((q, p) =>
    fetchJson(`/api/catalog/products?q=${encodeURIComponent(q)}&page=${p}`)
      .then(setList).catch(() => setError('Could not load products. Please refresh.')),
  [])

  useEffect(() => { loadStatus() }, [loadStatus])
  // Display names for profile values (mom → Mom) and the Edit modal's choices.
  useEffect(() => { fetchJson('/api/catalog/playground/options').then(setOptions).catch(() => {}) }, [])
  const labels = options
    ? Object.fromEntries(['recipients', 'occasions', 'vibes'].flatMap(k => options[k].map(o => [o.value, o.label])))
    : {}
  useEffect(() => { loadProducts(query, page) }, [loadProducts, page]) // eslint-disable-line react-hooks/exhaustive-deps

  // Poll while a sync runs; refresh the list once it finishes.
  const busy = status?.sync && IN_PROGRESS.includes(status.sync.status)
  useEffect(() => {
    if (!busy) return undefined
    const t = setInterval(() => { loadStatus(); loadProducts(query, page) }, POLL_MS)
    return () => clearInterval(t)
  }, [busy, loadStatus, loadProducts, query, page])

  // Re-enable Sync now when its cooldown ends.
  const nextManual = status?.next_manual_sync_at
  useEffect(() => {
    if (!nextManual) return undefined
    const t = setTimeout(loadStatus, Math.max(0, new Date(nextManual).getTime() - Date.now()) + 1000)
    return () => clearTimeout(t)
  }, [nextManual, loadStatus])

  function onSearch(value) {
    setQuery(value)
    clearTimeout(searchTimer.current)
    searchTimer.current = setTimeout(() => { setPage(1); loadProducts(value, 1) }, 300)
  }

  async function resync() {
    setResyncing(true)
    try {
      const res = await shopifyFetch('/api/catalog/resync', { method: 'POST' })
      if (!res.ok) {
        let json = {}
        try { json = await res.json() } catch { /* ignore */ }
        showToast(parseApiError(json).message, { isError: true })
        return
      }
      showToast('Sync started')
      loadStatus()
    } finally {
      setResyncing(false)
    }
  }

  async function openEditor(p) {
    if (!options) {
      try { setOptions(await fetchJson('/api/catalog/playground/options')) } catch { showToast('Could not load options.', { isError: true }); return }
    }
    setEditing(p)
  }

  function onProfileSaved(updated) {
    setList(l => ({ ...l, products: l.products.map(x => (x.product_id === updated.product_id ? updated : x)) }))
    setEditing(null)
    showToast(updated.overridden ? 'Gift profile saved' : 'Reset to AI suggestion')
  }

  async function toggleExcluded(p) {
    setToggling(p.product_id)
    try {
      const res = await shopifyFetch(`/api/catalog/products/${p.product_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ excluded: !p.excluded }),
      })
      if (!res.ok) { showToast('Could not update the product.', { isError: true }); return }
      const updated = await res.json()
      setList(l => ({ ...l, products: l.products.map(x => (x.product_id === p.product_id ? updated : x)) }))
      loadStatus()
      showToast(updated.excluded ? 'Excluded from gift suggestions' : 'Included in gift suggestions')
    } finally {
      setToggling(null)
    }
  }

  if (!status) {
    return (
      <Page title="Catalog">
        {error ? <Banner tone="critical" title={error} /> : <Card><SkeletonBodyText lines={6} /></Card>}
      </Page>
    )
  }

  const pages = list ? Math.max(1, Math.ceil(list.total / list.page_size)) : 1

  return (
    <Page
      title="Catalog"
      subtitle="What the gift finder knows about each product."
      primaryAction={{ content: 'Try the gift finder', onAction: () => navigate('/playground'), disabled: status.analyzed === 0 }}
    >
      <Layout>
        {error && (
          <Layout.Section>
            <Banner tone="critical" title={error} onDismiss={() => setError(null)} />
          </Layout.Section>
        )}
        <Layout.Section>
          <SyncCard status={status} onResync={resync} resyncing={resyncing} />
        </Layout.Section>
        <Layout.Section>
          <Card padding="0">
            <div style={{ padding: 'var(--p-space-300)' }}>
              <TextField
                label="Search products" labelHidden placeholder="Search by title or type"
                value={query} onChange={onSearch} autoComplete="off" clearButton onClearButtonClick={() => onSearch('')}
              />
            </div>
            {list && list.total === 0 ? (
              <EmptyState heading={query ? 'No matching products' : 'No products yet'} image="">
                <p>{query ? 'Try a different search.' : 'Products appear here after your first sync.'}</p>
              </EmptyState>
            ) : (
              <div>
                {!list && <div style={{ padding: 'var(--p-space-400)' }}><SkeletonBodyText lines={6} /></div>}
                {(list?.products || []).map(p => (
                  <ProductRow key={p.product_id} p={p} labels={labels} toggling={toggling === p.product_id} onToggle={toggleExcluded} onEdit={openEditor} />
                ))}
              </div>
            )}
            {pages > 1 && (
              <div style={{ padding: 'var(--p-space-300)', display: 'flex', justifyContent: 'center' }}>
                <Pagination
                  hasPrevious={page > 1} onPrevious={() => setPage(p => p - 1)}
                  hasNext={page < pages} onNext={() => setPage(p => p + 1)}
                  label={`Page ${page} of ${pages}`}
                />
              </div>
            )}
          </Card>
        </Layout.Section>
      </Layout>
      {editing && options && (
        <EditProfileModal product={editing} options={options} onClose={() => setEditing(null)} onSaved={onProfileSaved} />
      )}
    </Page>
  )
}
