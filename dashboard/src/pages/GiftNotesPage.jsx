import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Page, Layout, Card, BlockStack, Banner, SkeletonBodyText, Select, TextField, List, Button, InlineStack } from '@shopify/polaris'
import { shopifyFetch, fetchJson } from '../utils/shopifyFetch'
import { parseApiError } from '../utils/apiError'
import { showToast } from '../utils/toast'

function notesPayload(notes, bannedText) {
  const words = bannedText.split(',').map(w => w.trim().toLowerCase()).filter(Boolean)
  return { tone: notes.tone, max_chars: Number(notes.max_chars), banned_words: [...new Set(words)] }
}

export default function GiftNotesPage() {
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [notes, setNotes] = useState(null)
  const [bannedText, setBannedText] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchJson('/api/settings')
      .then(d => {
        setData(d)
        setNotes(d.gift_notes)
        setBannedText(d.gift_notes.banned_words.join(', '))
      })
      .catch(() => setError('Could not load gift note settings. Please refresh.'))
  }, [])

  async function save() {
    setSaving(true)
    setError(null)
    const payload = notesPayload(notes, bannedText)
    try {
      const res = await shopifyFetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gift_notes: payload }),
      })
      if (!res.ok) {
        let json = {}
        try { json = await res.json() } catch { /* ignore */ }
        setError(parseApiError(json).message)
        return
      }
      setData(d => ({ ...d, gift_notes: payload }))
      showToast('Gift notes saved')
    } catch {
      setError('Could not save. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  if (!data) {
    return (
      <Page title="Gift notes">
        {error ? <Banner tone="critical" title={error} /> : <Card><SkeletonBodyText lines={6} /></Card>}
      </Page>
    )
  }

  const dirty = JSON.stringify(notesPayload(notes, bannedText)) !== JSON.stringify(data.gift_notes)

  return (
    <Page
      title="Gift notes"
      subtitle="Shoppers add a personal note to each gift, with AI help if they want it."
      primaryAction={{ content: 'Save', onAction: save, loading: saving, disabled: !dirty }}
    >
      <Layout>
        {error && (
          <Layout.Section>
            <Banner tone="critical" title={error} onDismiss={() => setError(null)} />
          </Layout.Section>
        )}

        <Layout.AnnotatedSection
          title="AI drafts"
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
          title="Where shoppers see it"
          description="Notes are saved on the order and print on your GiftSense gift cards."
        >
          <Card>
            <BlockStack gap="300">
              <List>
                <List.Item>The gift panel, after “Add as a gift” in the gift finder or “This is a gift” on product pages.</List.Item>
                <List.Item>The cart page, with the Gift options block added to your cart template.</List.Item>
                <List.Item>Each draft uses one AI generation. Shoppers get 3 rewrites per gift.</List.Item>
              </List>
              <InlineStack>
                <Button onClick={() => navigate('/storefront')}>Set up blocks on your store</Button>
              </InlineStack>
            </BlockStack>
          </Card>
        </Layout.AnnotatedSection>
      </Layout>
    </Page>
  )
}
