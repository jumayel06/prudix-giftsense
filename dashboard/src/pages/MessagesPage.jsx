import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Page, Layout, Card, BlockStack, InlineStack, Text, Button, Banner, Badge, SkeletonBodyText, IndexTable,
  ProgressBar, EmptyState, Link,
} from '@shopify/polaris'
import { fetchJson } from '../utils/shopifyFetch'
import { showToast } from '../utils/toast'

// Optional: QR codes on Shopify's own packing slips. The order job writes the
// `giftsense.messages` order metafield, which packing-slip templates can read.
const PACKING_SLIP_SNIPPET = `{% assign gs_messages = order.metafields.giftsense.messages.value %}
{% if gs_messages %}
  <div style="margin-top: 16px; padding-top: 12px; border-top: 1px dashed #ccc;">
    {% for m in gs_messages %}
      <div style="display: inline-block; width: 120px; margin: 0 12px 8px 0; text-align: center; font-size: 11px;">
        <img src="{{ m.qr }}" width="100" height="100" alt=""><br>
        {% if m.for %}For {{ m.for }}: {% endif %}scan to {% if m.kind == 'video' %}watch{% else %}listen to{% endif %} your message
      </div>
    {% endfor %}
  </div>
{% endif %}`

const adminOrderUrl = id => `shopify://admin/orders/${id}`
const day = iso => new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
const secs = n => `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`

export default function MessagesPage() {
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchJson('/api/messages').then(setData).catch(() => setError('Could not load messages. Please refresh.'))
  }, [])

  if (!data) {
    return (
      <Page title="Voice & video">
        {error ? <Banner tone="critical" title={error} /> : <Card><SkeletonBodyText lines={6} /></Card>}
      </Page>
    )
  }

  if (!data.voice) {
    return (
      <Page title="Voice & video" subtitle="A recorded message the recipient plays by scanning a QR code on the gift card.">
        <Card>
          <BlockStack gap="300">
            <Text as="p">Shoppers record a voice message (or a video, on Pro) right in the gift panel. The printed gift card gets a QR code that opens the message on a page with your store&apos;s name.</Text>
            <Text as="p" tone="subdued">Voice messages are on the Growth plan; voice and video on Pro.</Text>
            <InlineStack><Button variant="primary" onClick={() => navigate('/plans')}>See plans</Button></InlineStack>
          </BlockStack>
        </Card>
      </Page>
    )
  }

  const pct = data.limit ? Math.min(100, Math.round((data.used / data.limit) * 100)) : 0
  const kinds = data.video ? 'voice and video messages' : 'voice messages'

  return (
    <Page title="Voice & video" subtitle="A recorded message the recipient plays by scanning a QR code on the gift card.">
      <Layout>
        {!data.available && (
          <Layout.Section>
            <Banner tone="warning" title="Messages aren't available on your storefront right now">
              <p>Recording is switched off while message storage is being set up. Shoppers don&apos;t see the option until then.</p>
            </Banner>
          </Layout.Section>
        )}
        <Layout.Section>
          <Card>
            <BlockStack gap="300">
              <InlineStack align="space-between" blockAlign="center">
                <Text as="h2" variant="headingMd">This month</Text>
                {data.available && <Badge tone="success">On in the gift panel</Badge>}
              </InlineStack>
              <Text as="p">{data.used.toLocaleString()} of {data.limit.toLocaleString()} {kinds} used</Text>
              <ProgressBar progress={pct} size="small" tone={pct >= 90 ? 'critical' : 'primary'} />
              <Text as="p" tone="subdued" variant="bodySm">
                Shoppers can record up to {secs(data.max_secs.voice)} of voice{data.video ? ` or ${secs(data.max_secs.video)} of video` : ''}.
                When the monthly limit is reached, the option is hidden until next month. Before recording,
                shoppers are told that anyone with the QR code or link can play the message.
              </Text>
            </BlockStack>
          </Card>
        </Layout.Section>
        <Layout.Section>
          <Card padding="0">
            {data.messages.length === 0 ? (
              <EmptyState heading="No messages yet" image="">
                <p>Messages show here once an order with a recording comes in. Print the order&apos;s gift cards (Print → GiftSense gift notes) to get the QR code.</p>
              </EmptyState>
            ) : (
              <IndexTable
                resourceName={{ singular: 'message', plural: 'messages' }}
                itemCount={data.messages.length}
                selectable={false}
                headings={[{ title: 'Order' }, { title: 'Type' }, { title: 'Length' }, { title: 'Plays' }, { title: 'Recorded' }, { title: 'Deleted on' }]}
              >
                {data.messages.map((m, i) => (
                  <IndexTable.Row id={String(i)} key={i} position={i}>
                    <IndexTable.Cell>
                      {m.order_id ? <Link url={adminOrderUrl(m.order_id)}>{m.order_name || `Order ${m.order_id}`}</Link> : '—'}
                    </IndexTable.Cell>
                    <IndexTable.Cell>{m.kind === 'video' ? 'Video' : 'Voice'}</IndexTable.Cell>
                    <IndexTable.Cell>{secs(m.duration_s)}</IndexTable.Cell>
                    <IndexTable.Cell>{m.views}</IndexTable.Cell>
                    <IndexTable.Cell>{m.created_at ? day(m.created_at) : '—'}</IndexTable.Cell>
                    <IndexTable.Cell>{m.expires_at ? day(m.expires_at) : '—'}</IndexTable.Cell>
                  </IndexTable.Row>
                ))}
              </IndexTable>
            )}
          </Card>
        </Layout.Section>
        <Layout.Section>
          <Card>
            <BlockStack gap="300">
              <Text as="h2" variant="headingMd">QR codes on your packing slips (optional)</Text>
              <Text as="p">
                GiftSense&apos;s own print action (order → Print → GiftSense gift notes) already prints the QR code on the gift card.
                If you pack with Shopify&apos;s packing slips (or a 3PL that uses them), you can add the QR codes there too:
                Shopify admin → Settings → Shipping and delivery → Packing slips → Edit, then paste this where it should appear
                (for example just before <code>{'{% endfor %}'}</code> of the items, or at the end).
              </Text>
              <pre style={{ margin: 0, padding: 12, background: 'var(--p-color-bg-surface-secondary)', borderRadius: 8, overflowX: 'auto', fontSize: 12 }}>{PACKING_SLIP_SNIPPET}</pre>
              <InlineStack gap="200">
                <Button onClick={() => { navigator.clipboard?.writeText(PACKING_SLIP_SNIPPET).then(() => showToast('Snippet copied')) }}>Copy snippet</Button>
              </InlineStack>
              <Text as="p" tone="subdued" variant="bodySm">Orders without a message print nothing extra. Use Preview in the template editor with a recent order that has a message.</Text>
            </BlockStack>
          </Card>
        </Layout.Section>
        <Layout.Section>
          <Text as="p" tone="subdued" variant="bodySm">
            Messages are deleted {data.retention_days} days after the gift&apos;s arrive-by date (or the order date), and recordings
            that never reach an order after 7 days. Uninstalling GiftSense deletes them all.
          </Text>
        </Layout.Section>
      </Layout>
    </Page>
  )
}
