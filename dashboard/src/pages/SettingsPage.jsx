import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Page, Layout, Card, BlockStack, InlineStack, Text, RadioButton, Checkbox,
  Button, Banner, SkeletonBodyText, Badge, Select, TextField,
} from '@shopify/polaris'
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

export default function SettingsPage() {
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [aiTier, setAiTier] = useState(null)
  const [digestOptIn, setDigestOptIn] = useState(true)
  const [notes, setNotes] = useState(null)
  const [bannedText, setBannedText] = useState('')
  const [wrap, setWrap] = useState(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchJson('/api/settings')
      .then(d => {
        setData(d)
        setAiTier(d.ai_tier)
        setDigestOptIn(d.digest_email_opt_in)
        setNotes(d.gift_notes)
        setBannedText(d.gift_notes.banned_words.join(', '))
        setWrap(wrapForm(d.gift_wrap))
      })
      .catch(() => setError('Could not load settings. Please refresh.'))
  }, [])

  // The wrap product is created in the background after a save: poll until it's live.
  const wrapPending = data?.gift_wrap?.enabled && data.gift_wrap.styles.length > 0
    && !data.gift_wrap.ready && !data.gift_wrap.error
  useEffect(() => {
    if (!wrapPending) return
    const t = setInterval(() => {
      fetchJson('/api/settings').then(d => setData(prev => ({ ...prev, gift_wrap: d.gift_wrap }))).catch(() => {})
    }, 3000)
    return () => clearInterval(t)
  }, [wrapPending])

  function notesPayload() {
    const words = bannedText.split(',').map(w => w.trim().toLowerCase()).filter(Boolean)
    return { tone: notes.tone, max_chars: Number(notes.max_chars), banned_words: [...new Set(words)] }
  }

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const res = await shopifyFetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ai_tier: aiTier, digest_email_opt_in: digestOptIn, gift_notes: notesPayload(),
          ...(wrapDirty ? { gift_wrap: wrapPayload(wrap) } : {}),
        }),
      })
      if (!res.ok) {
        let json = {}
        try { json = await res.json() } catch { /* ignore */ }
        setError(parseApiError(json).message)
        return
      }
      setData(d => ({ ...d, ai_tier: aiTier, digest_email_opt_in: digestOptIn, gift_notes: notesPayload() }))
      if (wrapDirty) {
        const d = await fetchJson('/api/settings')
        setData(prev => ({ ...prev, gift_wrap: d.gift_wrap }))
        setWrap(wrapForm(d.gift_wrap))
      }
      showToast('Settings saved')
    } catch {
      setError('Could not save settings. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  if (!data) {
    return (
      <Page title="Settings">
        {error ? <Banner tone="critical" title={error} /> : <Card><SkeletonBodyText lines={6} /></Card>}
      </Page>
    )
  }

  const hasWeeklyEmail = data.features.includes('weekly_email')
  const notesDirty = JSON.stringify(notesPayload()) !== JSON.stringify(data.gift_notes)
  const wrapDirty = JSON.stringify(wrapPayload(wrap)) !== JSON.stringify(wrapPayload(wrapForm(data.gift_wrap)))
  const setStyle = (i, field, value) =>
    setWrap(w => ({ ...w, styles: w.styles.map((s, j) => (j === i ? { ...s, [field]: value } : s)) }))
  const gw = data.gift_wrap
  const wrapStatus = !gw.enabled ? null
    : gw.error ? <Badge tone="critical">Setup failed</Badge>
    : gw.ready ? <Badge tone="success">Live on your store</Badge>
    : gw.styles.length ? <Badge tone="attention">Setting up…</Badge> : null
  const dirty = aiTier !== data.ai_tier || digestOptIn !== data.digest_email_opt_in || notesDirty || wrapDirty

  return (
    <Page
      title="Settings"
      primaryAction={{ content: 'Save', onAction: save, loading: saving, disabled: !dirty }}
    >
      <Layout>
        {error && (
          <Layout.Section>
            <Banner tone="critical" title={error} onDismiss={() => setError(null)} />
          </Layout.Section>
        )}

        <Layout.AnnotatedSection
          title="Plan"
          description="Your subscription. Downgrades take effect at the end of your billing month."
        >
          <Card>
            <BlockStack gap="300">
              <InlineStack align="space-between" blockAlign="center">
                <InlineStack gap="200" blockAlign="center">
                  <Text as="h3" variant="headingMd">{data.plan_name}</Text>
                  {data.plan_status === 'trial_active' && <Badge tone="success">Trial</Badge>}
                </InlineStack>
                <Text as="span" variant="bodyMd">${data.price_usd}/month</Text>
              </InlineStack>
              <Text as="p" tone="subdued">
                {data.generation_limit.toLocaleString()} AI generations per month
              </Text>
              {data.scheduled_plan_name && data.scheduled_change_at && (
                <Banner tone="info">
                  Your plan changes to {data.scheduled_plan_name} on {new Date(data.scheduled_change_at).toLocaleDateString()}.
                </Banner>
              )}
              <InlineStack>
                <Button onClick={() => navigate('/plans')}>Change plan</Button>
              </InlineStack>
            </BlockStack>
          </Card>
        </Layout.AnnotatedSection>

        <Layout.AnnotatedSection
          title="AI"
          description="Used for gift picks, gift notes and registry suggestions. Stronger options use more of your monthly generations per use."
        >
          <Card>
            <BlockStack gap="300">
              {data.ai_tiers_available.map(t => {
                const tier = data.ai_tiers[t]
                return (
                  <RadioButton
                    key={t}
                    id={`ai-tier-${t}`}
                    name="ai-tier"
                    label={tier.label}
                    helpText={
                      <BlockStack gap="050">
                        <span>{tier.description}</span>
                        {data.ai_tier_models?.[t] && (
                          <Text as="span" variant="bodySm" tone="subdued">
                            Currently runs on {data.ai_tier_models[t]}.
                          </Text>
                        )}
                      </BlockStack>
                    }
                    checked={aiTier === t}
                    onChange={() => setAiTier(t)}
                  />
                )
              })}
              <Text as="p" variant="bodySm" tone="subdued">
                We move each option to better AI models as they're released, at no extra cost to you.
                {data.plan_tier !== 'pro' && ' Stronger options are available on higher plans.'}
              </Text>
            </BlockStack>
          </Card>
        </Layout.AnnotatedSection>

        <Layout.AnnotatedSection
          title="Gift notes"
          description="How the AI drafts gift notes for shoppers. Shoppers can edit every draft before it's added to their order."
        >
          <Card>
            <BlockStack gap="400">
              <Select
                label="Default tone"
                options={data.note_tones.map(t => ({ label: t.label, value: t.value }))}
                value={notes.tone}
                onChange={tone => setNotes(n => ({ ...n, tone }))}
                helpText="Shoppers can switch tone for their own draft."
              />
              <TextField
                label="Maximum length (characters)" type="number" min={80} max={500}
                value={String(notes.max_chars)} onChange={v => setNotes(n => ({ ...n, max_chars: v }))}
                helpText="250 fits most printed gift cards." autoComplete="off"
              />
              <TextField
                label="Words the AI must never use" value={bannedText} onChange={setBannedText}
                placeholder="e.g. cheap, discount" helpText="Separate with commas." autoComplete="off"
              />
            </BlockStack>
          </Card>
        </Layout.AnnotatedSection>

        <Layout.AnnotatedSection
          title="Gift wrap"
          description="Offer paid or free gift wrap in the gift panel. GiftSense creates one hidden “Gift wrap” product in your store, with a variant per style. It never appears in your collections or search. Shoppers choose wrap themselves; it's never pre-selected."
        >
          <Card>
            <BlockStack gap="400">
              <InlineStack align="space-between" blockAlign="center">
                <Checkbox label="Offer gift wrap" checked={wrap.enabled}
                  onChange={enabled => setWrap(w => ({ ...w, enabled, styles: enabled && !w.styles.length ? [{ name: 'Gift wrap', price: '5' }] : w.styles }))} />
                {wrapStatus}
              </InlineStack>
              {gw.error && gw.enabled && (
                <Banner tone="critical" title="We couldn't set up the wrap product">
                  <p>{gw.error}. Save again to retry.</p>
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

        <Layout.AnnotatedSection
          title="Weekly sales email"
          description="A Monday summary of the sales GiftSense helped make."
        >
          <Card>
            {hasWeeklyEmail ? (
              <Checkbox
                label="Send me the weekly sales email"
                checked={digestOptIn}
                onChange={setDigestOptIn}
              />
            ) : (
              <BlockStack gap="200">
                <Text as="p" tone="subdued">Available on the Growth and Pro plans.</Text>
                <InlineStack><Button onClick={() => navigate('/plans')}>See plans</Button></InlineStack>
              </BlockStack>
            )}
          </Card>
        </Layout.AnnotatedSection>
      </Layout>
    </Page>
  )
}
