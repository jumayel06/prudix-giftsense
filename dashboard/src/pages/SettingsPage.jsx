import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Page, Layout, Card, BlockStack, InlineStack, Text, RadioButton, Checkbox,
  Button, Banner, SkeletonBodyText, Badge,
} from '@shopify/polaris'
import { shopifyFetch, fetchJson } from '../utils/shopifyFetch'
import { parseApiError } from '../utils/apiError'
import { modelLabel } from '../utils/modelLabels'
import { showToast } from '../utils/toast'

const MODEL_HINTS = {
  'gpt-6-luna':       'Fastest, and uses the fewest generations.',
  'claude-haiku-4-5': 'A solid all-rounder.',
  'gpt-6-sol':        'The most accurate gift reasons.',
  'claude-sonnet-5':  'Our strongest model for gift picks and notes.',
}

export default function SettingsPage() {
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [model, setModel] = useState(null)
  const [digestOptIn, setDigestOptIn] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchJson('/api/settings')
      .then(d => {
        setData(d)
        setModel(d.selected_model)
        setDigestOptIn(d.digest_email_opt_in)
      })
      .catch(() => setError('Could not load settings. Please refresh.'))
  }, [])

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const res = await shopifyFetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ selected_model: model, digest_email_opt_in: digestOptIn }),
      })
      if (!res.ok) {
        let json = {}
        try { json = await res.json() } catch { /* ignore */ }
        setError(parseApiError(json).message)
        return
      }
      setData(d => ({ ...d, selected_model: model, digest_email_opt_in: digestOptIn }))
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
  const dirty = model !== data.selected_model || digestOptIn !== data.digest_email_opt_in

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
          title="AI model"
          description="Used for gift picks, gift notes and registry suggestions. Stronger models use more of your monthly generations per use."
        >
          <Card>
            <BlockStack gap="300">
              {data.models_available.map(m => (
                <RadioButton
                  key={m}
                  id={`model-${m}`}
                  name="model"
                  label={`${modelLabel(m)} · ${data.model_weights[m]} generation${data.model_weights[m] === 1 ? '' : 's'} per use`}
                  helpText={MODEL_HINTS[m]}
                  checked={model === m}
                  onChange={() => setModel(m)}
                />
              ))}
              {data.plan_tier !== 'pro' && (
                <Text as="p" tone="subdued">
                  More models are available on higher plans.
                </Text>
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
