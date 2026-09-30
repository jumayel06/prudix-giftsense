import { useState } from 'react'
import { Modal, BlockStack, InlineStack, Text, Button, TextField, Banner } from '@shopify/polaris'
import { shopifyFetch } from '../utils/shopifyFetch'
import { parseApiError } from '../utils/apiError'

const MAX_VIBES = 5
const MAX_PITCH = 200

function Toggles({ label, options, value, onChange, max }) {
  const toggle = v => {
    if (value.includes(v)) onChange(value.filter(x => x !== v))
    else if (!max || value.length < max) onChange([...value, v])
  }
  return (
    <BlockStack gap="200">
      <Text as="span" fontWeight="semibold">{label}</Text>
      <InlineStack gap="200">
        {options.map(o => (
          <Button key={o.value} size="slim" pressed={value.includes(o.value)} onClick={() => toggle(o.value)}>{o.label}</Button>
        ))}
      </InlineStack>
    </BlockStack>
  )
}

/**
 * Edit what the gift finder believes about one product. Saves merchant
 * overrides (PATCH /api/catalog/products/{id}); the product is re-embedded
 * immediately, with no AI call. "Reset" returns to the AI's profile.
 */
export default function EditProfileModal({ product, options, onClose, onSaved }) {
  const prof = product.profile || {}
  const [recipients, setRecipients] = useState(prof.recipients || [])
  const [occasions, setOccasions] = useState(prof.occasions || [])
  const [vibes, setVibes] = useState(prof.vibes || [])
  const [pitch, setPitch] = useState(prof.gift_pitch || '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  async function save(overrides) {
    setSaving(true)
    setError(null)
    try {
      const res = await shopifyFetch(`/api/catalog/products/${product.product_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ overrides }),
      })
      let json = {}
      try { json = await res.json() } catch { /* ignore */ }
      if (!res.ok) { setError(parseApiError(json, 'Could not save. Please try again.').message); return }
      onSaved(json)
    } catch {
      setError('Could not save. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  const invalid = !recipients.length || !occasions.length

  return (
    <Modal
      open
      onClose={onClose}
      title={`Gift profile: ${product.title}`}
      primaryAction={{
        content: 'Save', loading: saving, disabled: invalid,
        onAction: () => save({ recipients, occasions, vibes, gift_pitch: pitch.trim() || null }),
      }}
      secondaryActions={[
        ...(product.overridden ? [{ content: 'Reset to AI suggestion', destructive: true, onAction: () => save(null) }] : []),
        { content: 'Cancel', onAction: onClose },
      ]}
    >
      <Modal.Section>
        <BlockStack gap="400">
          {error && <Banner tone="critical">{error}</Banner>}
          <Text as="p" tone="subdued">
            The gift finder uses this to decide when to suggest this product. Your edits replace the AI&apos;s suggestion
            and take effect right away.
          </Text>
          <Toggles label="Who is it a good gift for?" options={options.recipients} value={recipients} onChange={setRecipients} />
          <Toggles label="Occasions" options={options.occasions} value={occasions} onChange={setOccasions} />
          <Toggles label={`Vibes (up to ${MAX_VIBES})`} options={options.vibes} value={vibes} onChange={setVibes} max={MAX_VIBES} />
          <TextField
            label="Gift pitch"
            helpText="Our AI wrote this from your listing: one sentence on why it makes a good gift. The gift finder uses it to match this product to what shoppers ask for. Shoppers never see it, so edit it freely if it misses the point."
            value={pitch} onChange={setPitch} multiline={2} maxLength={MAX_PITCH} showCharacterCount autoComplete="off"
          />
          {invalid && <Text as="p" tone="critical">Pick at least one recipient and one occasion.</Text>}
        </BlockStack>
      </Modal.Section>
    </Modal>
  )
}
