import { useState, useEffect, useRef } from 'react'
import { Page, Layout, Card, BlockStack, InlineStack, Text, Checkbox, Button, Banner, SkeletonBodyText, Badge, TextField, Thumbnail, Select } from '@shopify/polaris'
import { ImageIcon } from '@shopify/polaris-icons'
import { shopifyFetch, fetchJson } from '../utils/shopifyFetch'
import { parseApiError } from '../utils/apiError'
import { showToast } from '../utils/toast'

const MAX_PACKAGING = 3
const MAX_CARDS = 2
const KINDS = [{ label: 'Wrap', value: 'wrap' }, { label: 'Bag', value: 'bag' }, { label: 'Box', value: 'box' }, { label: 'Greeting card', value: 'card' }]
const IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/gif']
const MAX_IMAGE_BYTES = 5 * 1024 * 1024

// Editable copy of the stored wrap settings (prices as strings for TextField).
// `image` is what we send back: the stored url/source keeps the photo, a new
// staged-upload url replaces it, null removes it. `preview` is just for display.
function wrapForm(w) {
  return {
    enabled: w.enabled,
    styles: w.styles.map(s => ({
      name: s.name, price: String(s.price), kind: s.kind || 'wrap',
      image: s.image_url || s.image_source || null, preview: s.image_url || null,
    })),
  }
}

function wrapPayload(form) {
  return {
    enabled: form.enabled,
    styles: form.styles.filter(s => s.name.trim())
      .map(s => ({ name: s.name.trim(), price: Number(s.price) || 0, kind: s.kind || 'wrap', image: s.image || null })),
  }
}

// Browser → Shopify staged upload (the file never passes through our server).
async function uploadPhoto(file) {
  if (!IMAGE_TYPES.includes(file.type)) throw new Error('Use a JPG, PNG, WebP or GIF image.')
  if (file.size > MAX_IMAGE_BYTES) throw new Error('Images can be up to 5 MB.')
  const res = await shopifyFetch('/api/settings/gift-wrap/image-upload', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: file.name, mime_type: file.type, size: file.size }),
  })
  let json = {}
  try { json = await res.json() } catch { /* ignore */ }
  if (!res.ok) throw new Error(parseApiError(json).message)
  const form = new FormData()
  json.parameters.forEach(p => form.append(p.name, p.value))
  form.append('file', file)
  const up = await fetch(json.url, { method: 'POST', body: form })
  if (!up.ok) throw new Error('The photo upload failed. Please try again.')
  return json.resource_url
}

function StylePhoto({ style, onChange, onError }) {
  const input = useRef(null)
  const [uploading, setUploading] = useState(false)

  async function pick(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setUploading(true)
    try {
      const image = await uploadPhoto(file)
      onChange({ image, preview: URL.createObjectURL(file) })
    } catch (err) {
      onError(err.message || 'The photo upload failed. Please try again.')
    } finally {
      setUploading(false)
    }
  }

  return (
    <InlineStack gap="200" blockAlign="center" wrap={false}>
      <Thumbnail size="small" alt={style.preview ? `${style.name} photo` : ''} source={style.preview || ImageIcon} />
      <input ref={input} type="file" accept={IMAGE_TYPES.join(',')} hidden onChange={pick} />
      <Button size="slim" loading={uploading} onClick={() => input.current?.click()}>
        {style.image ? 'Change photo' : 'Add photo'}
      </Button>
      {style.image && !uploading && (
        <Button size="slim" variant="plain" tone="critical" onClick={() => onChange({ image: null, preview: null })}>
          Remove photo
        </Button>
      )}
    </InlineStack>
  )
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
  const cards = wrap.styles.filter(s => s.kind === 'card').length
  const packaging = wrap.styles.length - cards
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
          description="GiftSense creates one hidden “Gift wrap” product in your store, with a variant per style. It never appears in your collections or search. Shoppers choose wrap themselves; it's never pre-selected. If you uninstall GiftSense, this product stays in your Products list, and you can delete it there."
        >
          <Card>
            <BlockStack gap="400">
              <InlineStack align="space-between" blockAlign="center">
                <Checkbox label="Offer gift wrap" checked={wrap.enabled}
                  onChange={enabled => setWrap(w => ({ ...w, enabled, styles: enabled && !w.styles.length ? [{ name: 'Gift wrap', price: '5', kind: 'wrap', image: null, preview: null }] : w.styles }))} />
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
                    <BlockStack key={i} gap="200">
                    <InlineStack gap="200" blockAlign="end" wrap={false}>
                      <div style={{ flex: 2 }}>
                        <TextField label="Style name" value={s.name} maxLength={40} autoComplete="off"
                          placeholder="e.g. Gold paper" onChange={v => setStyle(i, 'name', v)} />
                      </div>
                      <div style={{ flex: 1 }}>
                        <Select label="Type" value={s.kind || 'wrap'} onChange={v => setStyle(i, 'kind', v)}
                          options={KINDS.map(k => ({ ...k, disabled: k.value !== s.kind && (k.value === 'card' ? cards >= MAX_CARDS : s.kind === 'card' && packaging >= MAX_PACKAGING) }))} />
                      </div>
                      <div style={{ flex: 1 }}>
                        <TextField label="Price" type="number" min={0} max={1000}
                          step={0.5} value={s.price} autoComplete="off" onChange={v => setStyle(i, 'price', v)} />
                      </div>
                      <Button variant="plain" tone="critical" accessibilityLabel={`Remove ${s.name || 'style'}`}
                        onClick={() => setWrap(w => ({ ...w, styles: w.styles.filter((_, j) => j !== i) }))}>Remove</Button>
                    </InlineStack>
                    <StylePhoto style={s} onError={setError}
                      onChange={patch => setWrap(w => ({ ...w, styles: w.styles.map((x, j) => (j === i ? { ...x, ...patch } : x)) }))} />
                    </BlockStack>
                  ))}
                  <InlineStack gap="200">
                    {packaging < MAX_PACKAGING && (
                      <Button onClick={() => setWrap(w => ({ ...w, styles: [...w.styles, { name: '', price: '0', kind: 'wrap', image: null, preview: null }] }))}>
                        Add wrap style
                      </Button>
                    )}
                    {cards < MAX_CARDS && (
                      <Button onClick={() => setWrap(w => ({ ...w, styles: [...w.styles, { name: '', price: '0', kind: 'card', image: null, preview: null }] }))}>
                        Add greeting card
                      </Button>
                    )}
                  </InlineStack>
                  <Text as="p" tone="subdued" variant="bodySm">
                    Some products can&apos;t be wrapped (oversized, digital)? Add the tag <b>no-gift-wrap</b> to them in Shopify admin → Products, and the gift panel won&apos;t offer wrap for them.
                  </Text>
                  <Text as="p" tone="subdued" variant="bodySm">
                    Up to 3 wrap styles (gift wrap, a gift bag or a gift box) and up to 2 greeting cards, offered next to the wrap choice. Set a price of 0 for free. Photos (optional, JPG/PNG/WebP up to 5 MB) show next to each style in the gift panel. Shoppers can also add wrap for the whole order from the cart page or cart drawer. Wrap is added as its own line in the cart, linked to the gift.
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
