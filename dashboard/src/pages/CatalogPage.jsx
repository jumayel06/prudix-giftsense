import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Page, Layout, Card, BlockStack, InlineStack, Text, Banner, ProgressBar, Badge,
  IndexTable, Thumbnail, TextField, Pagination, Button, SkeletonBodyText, EmptyState,
} from '@shopify/polaris'
import { ImageIcon } from '@shopify/polaris-icons'
import { shopifyFetch, fetchJson } from '../utils/shopifyFetch'
import { parseApiError } from '../utils/apiError'
import { showToast } from '../utils/toast'

const POLL_MS = 5000
const IN_PROGRESS = ['running', 'importing']

function money(n) {
  return `$${Number(n).toFixed(2).replace(/\.00$/, '')}`
}

function SyncCard({ status, onResync, resyncing }) {
  const sync = status.sync
  const busy = sync && IN_PROGRESS.includes(sync.status)
  const pct = busy && sync.total ? Math.round((100 * sync.enriched) / sync.total) : null

  return (
    <Card>
      <BlockStack gap="300">
        <InlineStack align="space-between" blockAlign="center">
          <Text as="h2" variant="headingMd">Catalog analysis</Text>
          <Button onClick={onResync} loading={resyncing} disabled={busy}>Sync now</Button>
        </InlineStack>

        {busy && (
          <BlockStack gap="200">
            <Text as="p">
              {sync.status === 'running'
                ? 'Reading your products from Shopify…'
                : `Analyzing your catalog… ${sync.enriched} of ${sync.total}`}
            </Text>
            <ProgressBar progress={pct ?? 5} size="small" />
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

function ProfileCell({ p }) {
  if (p.excluded) return <Badge>Excluded</Badge>
  if (!p.analyzed) return <Badge tone="attention">Analyzing</Badge>
  const prof = p.profile || {}
  return (
    <BlockStack gap="100">
      <Text as="span" variant="bodySm">{prof.gift_pitch}</Text>
      <InlineStack gap="100">
        {(prof.vibes || []).slice(0, 3).map(v => <Badge key={v} tone="info">{v}</Badge>)}
        {(prof.recipients || []).slice(0, 2).map(r => <Badge key={r}>{r}</Badge>)}
        {p.profile_fallback && <Badge tone="warning">Basic profile</Badge>}
      </InlineStack>
    </BlockStack>
  )
}

export default function CatalogPage() {
  const [status, setStatus] = useState(null)
  const [list, setList] = useState(null)
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [error, setError] = useState(null)
  const [resyncing, setResyncing] = useState(false)
  const [toggling, setToggling] = useState(null)
  const searchTimer = useRef(null)

  const loadStatus = useCallback(() =>
    fetchJson('/api/catalog/status').then(setStatus).catch(() => setError('Could not load your catalog. Please refresh.')),
  [])

  const loadProducts = useCallback((q, p) =>
    fetchJson(`/api/catalog/products?q=${encodeURIComponent(q)}&page=${p}`)
      .then(setList).catch(() => setError('Could not load products. Please refresh.')),
  [])

  useEffect(() => { loadStatus() }, [loadStatus])
  useEffect(() => { loadProducts(query, page) }, [loadProducts, page]) // eslint-disable-line react-hooks/exhaustive-deps

  // Poll while a sync runs; refresh the list once it finishes.
  const busy = status?.sync && IN_PROGRESS.includes(status.sync.status)
  useEffect(() => {
    if (!busy) return undefined
    const t = setInterval(() => { loadStatus(); loadProducts(query, page) }, POLL_MS)
    return () => clearInterval(t)
  }, [busy, loadStatus, loadProducts, query, page])

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
      setTimeout(loadStatus, 1500)
    } finally {
      setResyncing(false)
    }
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
    <Page title="Catalog" subtitle="What the gift finder knows about each product.">
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
              <IndexTable
                resourceName={{ singular: 'product', plural: 'products' }}
                itemCount={list?.products.length || 0}
                selectable={false}
                loading={!list}
                headings={[{ title: '' }, { title: 'Product' }, { title: 'Price' }, { title: 'Gift profile' }, { title: '' }]}
              >
                {(list?.products || []).map((p, i) => (
                  <IndexTable.Row id={p.product_id} key={p.product_id} position={i}>
                    <IndexTable.Cell>
                      <Thumbnail source={p.image_url || ImageIcon} alt={p.title} size="small" />
                    </IndexTable.Cell>
                    <IndexTable.Cell>
                      <BlockStack gap="050">
                        <Text as="span" fontWeight="semibold">{p.title}</Text>
                        <InlineStack gap="100">
                          {p.product_type && <Text as="span" variant="bodySm" tone="subdued">{p.product_type}</Text>}
                          {!p.available && <Badge tone="critical">Out of stock</Badge>}
                        </InlineStack>
                      </BlockStack>
                    </IndexTable.Cell>
                    <IndexTable.Cell>
                      {p.price_min === p.price_max ? money(p.price_min) : `${money(p.price_min)}–${money(p.price_max)}`}
                    </IndexTable.Cell>
                    <IndexTable.Cell><ProfileCell p={p} /></IndexTable.Cell>
                    <IndexTable.Cell>
                      <Button variant="plain" loading={toggling === p.product_id} onClick={() => toggleExcluded(p)}>
                        {p.excluded ? 'Include' : 'Exclude'}
                      </Button>
                    </IndexTable.Cell>
                  </IndexTable.Row>
                ))}
              </IndexTable>
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
    </Page>
  )
}
