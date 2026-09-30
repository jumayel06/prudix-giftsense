import { useState, useEffect } from 'react'
import { Page, Layout, Card, BlockStack, InlineStack, Text, Checkbox, Button, Banner, SkeletonBodyText, Badge, TextField } from '@shopify/polaris'
import { shopifyFetch, fetchJson } from '../utils/shopifyFetch'
import { parseApiError } from '../utils/apiError'
import { showToast } from '../utils/toast'

const MAX_WRAP_STYLES = 3

// Editable copy of the stored wrap settings (prices as strings for TextField).
function wrapForm(w) {
  return { enabled: w.enabled, styles: w.styles.map(s => ({ name: s.name, price: String(s.price) })) }
}

function wrapPayload(form) {
  return {
    enabled: form.enabled,
    styles: form.styles.filter(s => s.name.trim()).map(s => ({ name: s.name.trim(), price: Number(s.price) || 0 })),
  }
}

export default function GiftWrapPage() {
  const [stored, setStored] = useState(null)
  const [wrap, setWrap] = useState(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchJson('/api/settings')
      .then(d => { setStored(d.gift_wrap); setWrap(wrapForm(d.gift_wrap)) })
      .catch(() => setError('Could not load gift wrap settings. Please refresh.'))
  }, [])

  // The wrap product is created in the background after a save: poll until it's live.
  const pending = stored?.enabled && stored.styles.length > 0 && !stored.ready && !stored.error
  useEffect(() => {
    if (!pending) return
    const t = setInterval(() => {
      fetchJson('/api/settings').then(d => setStored(d.gift_wrap)).catch(() => {})
    }, 3000)
    return () => clearInterval(t)
  }, [pending])

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const res = await shopifyFetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gift_wrap: wrapPayload(wrap) }),
      })
      if (!res.ok) {
        let json = {}
        try { json = await res.json() } catch { /* ignore */ }
        setError(parseApiError(json).message)
        return
      }
      const d = await fetchJson('/api/settings')
      setStored(d.gift_wrap)
      setWrap(wrapForm(d.gift_wrap))
      showToast('Gift wrap saved')
    } catch {
      setError('Could not save. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  if (!stored) {
    return (
      <Page title="Gift wrap">
        {error ? <Banner tone="critical" title={error} /> : <Card><SkeletonBodyText lines={6} /></Card>}
      </Page>
    )
  }

  const dirty = JSON.stringify(wrapPayload(wrap)) !== JSON.stringify(wrapPayload(wrapForm(stored)))
  const setStyle = (i, field, value) =>
    setWrap(w => ({ ...w, styles: w.styles.map((s, j) => (j === i ? { ...s, [field]: value } : s)) }))
  const status = !stored.enabled ? null
    : stored.error ? <Badge tone="critical">Setup failed</Badge>
    : stored.ready ? <Badge tone="success">Live on your store</Badge>
    : stored.styles.length ? <Badge tone="attention">Setting up…</Badge> : null

  return (
    <Page
      title="Gift wrap"
      subtitle="Offer your own wrap styles as an add-on in the gift panel."
      primaryAction={{ content: 'Save', onAction: save, loading: saving, disabled: !dirty }}
    >
      <Layout>
        {error && (
          <Layout.Section>
            <Banner tone="critical" title={error} onDismiss={() => setError(null)} />
          </Layout.Section>
        )}

        <Layout.AnnotatedSection
          title="Wrap styles"
          description="GiftSense creates one hidden “Gift wrap” product in your store, with a variant per style. It never appears in your collections or search. Shoppers choose wrap themselves; it's never pre-selected."
        >
          <Card>
            <BlockStack gap="400">
              <InlineStack align="space-between" blockAlign="center">
                <Checkbox label="Offer gift wrap" checked={wrap.enabled}
                  onChange={enabled => setWrap(w => ({ ...w, enabled, styles: enabled && !w.styles.length ? [{ name: 'Gift wrap', price: '5' }] : w.styles }))} />
                {status}
              </InlineStack>
              {stored.error && stored.enabled && (
                <Banner tone="critical" title="We couldn't set up the wrap product">
                  <p>{stored.error}. Save again to retry.</p>
                </Banner>
              )}
              {wrap.enabled && (
                <BlockStack gap="300">
                  {wrap.styles.map((s, i) => (
                    <InlineStack key={i} gap="200" blockAlign="end" wrap={false}>
                      <div style={{ flex: 2 }}>
                        <TextField label="Style name" value={s.name} maxLength={40} autoComplete="off"
                          placeholder="e.g. Gold paper" onChange={v => setStyle(i, 'name', v)} />
                      </div>
                      <div style={{ flex: 1 }}>
                        <TextField label="Price" type="number" min={0} max={1000}
                          step={0.5} value={s.price} autoComplete="off" onChange={v => setStyle(i, 'price', v)} />
                      </div>
                      <Button variant="plain" tone="critical" accessibilityLabel={`Remove ${s.name || 'style'}`}
                        onClick={() => setWrap(w => ({ ...w, styles: w.styles.filter((_, j) => j !== i) }))}>Remove</Button>
                    </InlineStack>
                  ))}
                  {wrap.styles.length < MAX_WRAP_STYLES && (
                    <InlineStack>
                      <Button onClick={() => setWrap(w => ({ ...w, styles: [...w.styles, { name: '', price: '0' }] }))}>
                        Add style
                      </Button>
                    </InlineStack>
                  )}
                  <Text as="p" tone="subdued" variant="bodySm">
                    Up to 3 styles. Set a price of 0 for free wrap. Wrap is added as its own line in the cart, linked to the gift.
                  </Text>
                </BlockStack>
              )}
            </BlockStack>
          </Card>
        </Layout.AnnotatedSection>
      </Layout>
    </Page>
  )
}
