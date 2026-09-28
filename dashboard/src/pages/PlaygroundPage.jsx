import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Page, Layout, Card, BlockStack, InlineStack, InlineGrid, Text, Select, TextField, Button, Banner,
  Badge, Thumbnail, SkeletonBodyText, Box, Tag, Link,
} from '@shopify/polaris'
import { ImageIcon } from '@shopify/polaris-icons'
import { shopifyFetch, fetchJson } from '../utils/shopifyFetch'
import { parseApiError } from '../utils/apiError'
import { modelLabel } from '../utils/modelLabels'

function money(n) {
  return `$${Number(n).toFixed(2).replace(/\.00$/, '')}`
}

function VibePicker({ options, value, max, onChange }) {
  const toggle = v => onChange(value.includes(v) ? value.filter(x => x !== v) : [...value, v].slice(-max))
  return (
    <BlockStack gap="200">
      <Text as="span">They are… <Text as="span" tone="subdued">(up to {max})</Text></Text>
      <InlineStack gap="200">
        {options.map(o => (
          <Button key={o.value} size="slim" pressed={value.includes(o.value)} onClick={() => toggle(o.value)}>
            {o.label}
          </Button>
        ))}
      </InlineStack>
    </BlockStack>
  )
}

function PickCard({ pick, rank }) {
  return (
    <Card>
      <InlineStack gap="400" blockAlign="start" wrap={false}>
        <Thumbnail source={pick.image_url || ImageIcon} alt={pick.title} size="large" />
        <BlockStack gap="150">
          <InlineStack gap="200" blockAlign="center">
            <Text as="span" tone="subdued">#{rank}</Text>
            {pick.url
              ? <Link url={pick.url} target="_blank"><Text as="span" fontWeight="semibold">{pick.title}</Text></Link>
              : <Text as="span" fontWeight="semibold">{pick.title}</Text>}
          </InlineStack>
          <Text as="span">
            {pick.price_min === pick.price_max ? money(pick.price_min) : `${money(pick.price_min)}–${money(pick.price_max)}`}
          </Text>
          <Text as="p">{pick.reason}</Text>
          {pick.source === 'template' && (
            <InlineStack><Badge tone="info">Standard reason</Badge></InlineStack>
          )}
        </BlockStack>
      </InlineStack>
    </Card>
  )
}

export default function PlaygroundPage() {
  const navigate = useNavigate()
  const [opts, setOpts] = useState(null)
  const [brief, setBrief] = useState({ recipient: 'friend', occasion: 'birthday', budget_band: '25_50', vibes: [], age_band: '', free_text: '' })
  const [result, setResult] = useState(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchJson('/api/catalog/playground/options').then(setOpts).catch(() => setError('Could not load the form. Please refresh.'))
  }, [])

  const set = key => value => setBrief(b => ({ ...b, [key]: value }))

  async function run() {
    setRunning(true)
    setError(null)
    try {
      const body = { ...brief, age_band: brief.age_band || null }
      const res = await shopifyFetch('/api/catalog/playground', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      let json = {}
      try { json = await res.json() } catch { /* ignore */ }
      if (!res.ok) { setError(parseApiError(json).message); return }
      setResult(json)
    } catch {
      setError('Search failed. Please try again.')
    } finally {
      setRunning(false)
    }
  }

  if (!opts) {
    return (
      <Page title="Try the gift finder" backAction={{ content: 'Catalog', onAction: () => navigate('/catalog') }}>
        {error ? <Banner tone="critical" title={error} /> : <Card><SkeletonBodyText lines={8} /></Card>}
      </Page>
    )
  }

  const choices = list => list.map(o => ({ label: o.label, value: o.value }))

  return (
    <Page
      title="Try the gift finder"
      subtitle="Search your own catalog the way a shopper would. Test searches don't use your plan's generations."
      backAction={{ content: 'Catalog', onAction: () => navigate('/catalog') }}
    >
      <Layout>
        <Layout.Section variant="oneThird">
          <Card>
            <BlockStack gap="400">
              <Select label="Who is it for?" options={choices(opts.recipients)} value={brief.recipient} onChange={set('recipient')} />
              <Select label="Occasion" options={choices(opts.occasions)} value={brief.occasion} onChange={set('occasion')} />
              <Select label="Budget" options={choices(opts.budgets)} value={brief.budget_band} onChange={set('budget_band')} />
              <Select
                label="Age (optional)"
                options={[{ label: 'Any', value: '' }, ...choices(opts.age_bands.filter(a => a.value !== 'any'))]}
                value={brief.age_band} onChange={set('age_band')}
              />
              <VibePicker options={opts.vibes} value={brief.vibes} max={opts.max_vibes} onChange={set('vibes')} />
              <TextField
                label="Anything else? (optional)" value={brief.free_text} onChange={set('free_text')}
                maxLength={200} showCharacterCount multiline={2} autoComplete="off"
                placeholder="e.g. loves hiking and strong coffee"
              />
              <Button variant="primary" onClick={run} loading={running} fullWidth>Find gifts</Button>
            </BlockStack>
          </Card>
        </Layout.Section>

        <Layout.Section>
          <BlockStack gap="300">
            {error && <Banner tone="critical" title={error} onDismiss={() => setError(null)} />}

            {!result && !error && (
              <Card>
                <Box padding="400">
                  <Text as="p" tone="subdued" alignment="center">Pick a recipient and occasion, then click Find gifts.</Text>
                </Box>
              </Card>
            )}

            {result && result.picks.length === 0 && (
              <Banner tone="warning" title="No gifts found">
                <p>Nothing in your analyzed catalog fits this budget and recipient. Try another budget, or check the Catalog page for products still being analyzed.</p>
              </Banner>
            )}

            {result && result.picks.length > 0 && (
              <>
                <InlineStack gap="200">
                  <Tag>{modelLabel(result.model)}</Tag>
                  <Tag>{(result.latency_ms / 1000).toFixed(1)} s</Tag>
                  <Tag>{result.candidates_considered} products considered</Tag>
                  {result.mode === 'small_catalog' && <Tag>Small-catalog mode</Tag>}
                </InlineStack>
                {result.used_fallback && (
                  <Banner tone="info"><p>The AI didn't answer in time, so these picks use standard reasons. Shoppers never see an empty result.</p></Banner>
                )}
                <InlineGrid columns={1} gap="300">
                  {result.picks.map((p, i) => <PickCard key={p.product_id} pick={p} rank={i + 1} />)}
                </InlineGrid>
              </>
            )}
          </BlockStack>
        </Layout.Section>
      </Layout>
    </Page>
  )
}
