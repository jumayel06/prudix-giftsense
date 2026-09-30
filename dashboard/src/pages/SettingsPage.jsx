import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Page, Layout, Card, BlockStack, InlineStack, Text, RadioButton, Checkbox,
  Button, Banner, SkeletonBodyText, Badge,
} from '@shopify/polaris'
import { shopifyFetch, fetchJson } from '../utils/shopifyFetch'
import { parseApiError } from '../utils/apiError'
import { showToast } from '../utils/toast'

export default function SettingsPage() {
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [aiTier, setAiTier] = useState(null)
  const [digestOptIn, setDigestOptIn] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchJson('/api/settings')
      .then(d => {
        setData(d)
        setAiTier(d.ai_tier)
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
        body: JSON.stringify({ ai_tier: aiTier, digest_email_opt_in: digestOptIn }),
      })
      if (!res.ok) {
        let json = {}
        try { json = await res.json() } catch { /* ignore */ }
        setError(parseApiError(json).message)
        return
      }
      setData(d => ({ ...d, ai_tier: aiTier, digest_email_opt_in: digestOptIn }))
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
  const dirty = aiTier !== data.ai_tier || digestOptIn !== data.digest_email_opt_in

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
              {Object.keys(data.ai_tiers).map(t => {
                const tier = data.ai_tiers[t]
                const locked = !data.ai_tiers_available.includes(t)
                return (
                  <RadioButton
                    key={t}
                    id={`ai-tier-${t}`}
                    name="ai-tier"
                    disabled={locked}
                    label={
                      <InlineStack gap="200" blockAlign="center">
                        <span>{tier.label}</span>
                        {locked && tier.plan && <Badge tone="info" size="small">{`Available on ${tier.plan}`}</Badge>}
                      </InlineStack>
                    }
                    helpText={
                      <BlockStack gap="050">
                        <span>{tier.description}</span>
                        {!locked && data.ai_tier_models?.[t] && (
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
              </Text>
              {data.ai_tiers_available.length < Object.keys(data.ai_tiers).length && (
                <InlineStack><Button onClick={() => navigate('/plans')}>See plans</Button></InlineStack>
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
