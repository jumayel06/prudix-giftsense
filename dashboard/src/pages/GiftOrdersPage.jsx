import { useState, useEffect } from 'react'
import { Page, Card, IndexTable, Text, Badge, Banner, SkeletonBodyText, BlockStack, InlineStack, Link, EmptyState } from '@shopify/polaris'
import { fetchJson } from '../utils/shopifyFetch'

function money(amount, currency) {
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency || 'USD' }).format(amount)
  } catch {
    return `${currency} ${Number(amount).toFixed(2)}`
  }
}

const NOTE_LABEL = { ai_accepted: 'AI note', ai_edited: 'AI note, edited', manual: 'Own note' }

// Opens the order in Shopify admin (App Bridge handles shopify:// links).
const adminOrderUrl = id => `shopify://admin/orders/${id}`

export default function GiftOrdersPage() {
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchJson(`/api/gift-orders?page=${page}`)
      .then(setData)
      .catch(() => setError('Could not load gift orders. Please refresh.'))
  }, [page])

  const help = (
    <Text as="p" tone="subdued">
      To print gift cards or a price-free gift receipt, open an order and choose Print → GiftSense gift cards.
      To print several at once, select orders in your Orders list and choose Print → GiftSense gift cards (bulk).
    </Text>
  )

  if (!data) {
    return (
      <Page title="Gift orders">
        {error ? <Banner tone="critical" title={error} /> : <Card><SkeletonBodyText lines={8} /></Card>}
      </Page>
    )
  }

  const rows = data.orders.map((o, i) => (
    <IndexTable.Row id={o.order_id} key={o.order_id} position={i}>
      <IndexTable.Cell>
        <BlockStack gap="050">
          <Link url={adminOrderUrl(o.order_id)} target="_top" removeUnderline>
            <Text as="span" fontWeight="semibold">{o.order_name || `#${o.order_id}`}</Text>
          </Link>
          <Text as="span" variant="bodySm" tone="subdued">{new Date(o.created_at).toLocaleDateString()}</Text>
        </BlockStack>
      </IndexTable.Cell>
      <IndexTable.Cell>
        {o.mode === 'self'
          ? (o.recipients.length ? o.recipients.join(', ') : 'To the shopper')
          : 'Ships to recipient'}
      </IndexTable.Cell>
      <IndexTable.Cell>{o.has_note ? NOTE_LABEL[o.note_source] || 'Note' : <Text as="span" tone="subdued">None</Text>}</IndexTable.Cell>
      <IndexTable.Cell>{o.wraps.length ? o.wraps.join(', ') : <Text as="span" tone="subdued">None</Text>}</IndexTable.Cell>
      <IndexTable.Cell>
        <Text as="span" alignment="end" numeric>{money(o.gift_revenue, o.currency)}</Text>
      </IndexTable.Cell>
      <IndexTable.Cell>
        <InlineStack gap="100">
          {o.from_finder && <Badge tone="info">Gift finder</Badge>}
          {!o.annotated && <Badge tone="attention">Tagging…</Badge>}
        </InlineStack>
      </IndexTable.Cell>
    </IndexTable.Row>
  ))

  return (
    <Page title="Gift orders" subtitle={`${data.total} gift order${data.total === 1 ? '' : 's'}`}>
      <BlockStack gap="400">
        {error && <Banner tone="critical" title={error} onDismiss={() => setError(null)} />}
        {data.total === 0 ? (
          <Card>
            <EmptyState heading="No gift orders yet" image="">
              <p>Orders where shoppers mark items as gifts, add a gift note or choose gift wrap show up here.</p>
            </EmptyState>
          </Card>
        ) : (
          <Card padding="0">
            <IndexTable
              resourceName={{ singular: 'gift order', plural: 'gift orders' }}
              itemCount={data.orders.length}
              selectable={false}
              headings={[
                { title: 'Order' }, { title: 'Gift for' }, { title: 'Note' }, { title: 'Wrap' },
                { title: 'Gift value', alignment: 'end' }, { title: '' },
              ]}
              pagination={{
                hasPrevious: page > 1, onPrevious: () => setPage(p => p - 1),
                hasNext: data.has_next, onNext: () => setPage(p => p + 1),
              }}
            >
              {rows}
            </IndexTable>
          </Card>
        )}
        <Card>{help}</Card>
      </BlockStack>
    </Page>
  )
}
