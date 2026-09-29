import { useState, useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Page, Layout, Card, BlockStack, InlineStack, Text, Badge, Banner, Button } from '@shopify/polaris'
import { fetchJson } from '../utils/shopifyFetch'
import { UPCOMING_BY_PATH } from '../utils/upcoming'

// One placeholder page for every planned section (dashboard/src/utils/upcoming.js).
export default function ComingSoonPage({ planTier }) {
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const section = UPCOMING_BY_PATH[pathname]
  const [plans, setPlans] = useState(null)

  useEffect(() => {
    fetchJson('/api/plans').then(d => setPlans(d.plans)).catch(() => setPlans([]))
  }, [])

  if (!section) return null

  const plansWith = feature => (plans || []).filter(p => p.features.includes(feature))
  const inMyPlan = feature => plansWith(feature).some(p => p.tier === planTier)
  const upgradeNeeded = plans && section.items.some(i => !inMyPlan(i.feature))

  return (
    <Page title={section.title} titleMetadata={<Badge tone="info">Coming soon</Badge>}>
      <Layout>
        <Layout.Section>
          <Card>
            <BlockStack gap="400">
              <Text as="p" variant="bodyLg">{section.intro}</Text>
              <BlockStack gap="300">
                {section.items.map(i => {
                  const names = plansWith(i.feature).map(p => p.name)
                  return (
                    <InlineStack key={i.feature} align="space-between" blockAlign="center" gap="300" wrap={false}>
                      <Text as="span">{i.text}</Text>
                      {plans && (inMyPlan(i.feature)
                        ? <Badge tone="success">In your plan</Badge>
                        : <Badge>{names.length === 1 ? `${names[0]} plan` : names.join(' and ')}</Badge>)}
                    </InlineStack>
                  )
                })}
              </BlockStack>
            </BlockStack>
          </Card>
        </Layout.Section>
        <Layout.Section>
          <Banner tone="info" title="We're building this now">
            <p>
              It will appear here automatically when it's ready, with nothing to install.
              {upgradeNeeded && ' Some of it needs a higher plan.'}
            </p>
            {upgradeNeeded && (
              <div style={{ marginTop: 'var(--p-space-200)' }}>
                <Button onClick={() => navigate('/plans')}>See plans</Button>
              </div>
            )}
          </Banner>
        </Layout.Section>
      </Layout>
    </Page>
  )
}
