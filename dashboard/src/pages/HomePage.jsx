import { useNavigate } from 'react-router-dom'
import { Page, Layout, Card, BlockStack, InlineStack, Text, ProgressBar, Button, Banner, Badge } from '@shopify/polaris'
import { modelLabel } from '../utils/modelLabels'

// Home for the week-1 shell: plan + usage. The onboarding checklist (enable
// the widget, catalog analysis, wrap setup) arrives with the gift finder.

function UsageCard({ stats }) {
  const isTrial = stats.plan_status === 'trial_active'
  const limit = isTrial ? stats.trial_generations_cap : stats.generation_limit
  const used = isTrial ? stats.trial_generations_used : stats.generations_used
  const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0
  const tone = pct >= 100 ? 'critical' : pct >= 75 ? 'highlight' : 'primary'

  return (
    <Card>
      <BlockStack gap="300">
        <InlineStack align="space-between" blockAlign="center">
          <Text as="h2" variant="headingMd">AI generations</Text>
          {isTrial
            ? <Badge tone="success">{`Trial · ${stats.trial_days_remaining} day${stats.trial_days_remaining === 1 ? '' : 's'} left`}</Badge>
            : <Badge>{`${stats.days_remaining} days left in cycle`}</Badge>}
        </InlineStack>
        <ProgressBar progress={pct} tone={tone} size="small" />
        <Text as="p" tone="subdued">
          {used.toLocaleString()} of {limit.toLocaleString()} used{isTrial ? ' in your trial' : ' this month'}
          {' · '}model: {modelLabel(stats.selected_model)} ({stats.model_weight} per use)
        </Text>
      </BlockStack>
    </Card>
  )
}

export default function HomePage({ stats }) {
  const navigate = useNavigate()

  if (!stats) {
    return (
      <Page title="Prudix GiftSense">
        <Banner tone="critical" title="Could not load your account. Please refresh the page." />
      </Page>
    )
  }

  return (
    <Page title="Prudix GiftSense" subtitle={`${stats.plan_name} plan`}>
      <Layout>
        {stats.plan_status === 'cancelled' && stats.access_until && (
          <Layout.Section>
            <Banner tone="warning" title={`Your plan is cancelled. Access continues until ${new Date(stats.access_until).toLocaleDateString()}.`}>
              <Button onClick={() => navigate('/plans')}>Choose a plan</Button>
            </Banner>
          </Layout.Section>
        )}
        {stats.scheduled_plan_name && stats.scheduled_change_at && (
          <Layout.Section>
            <Banner tone="info" title={`Your plan changes to ${stats.scheduled_plan_name} on ${new Date(stats.scheduled_change_at).toLocaleDateString()}.`} />
          </Layout.Section>
        )}
        {stats.show_review_prompt && stats.review_prompt_url && (
          <Layout.Section>
            <Banner tone="success" title="Enjoying GiftSense?">
              <Button url={stats.review_prompt_url} target="_blank">Leave a review</Button>
            </Banner>
          </Layout.Section>
        )}
        <Layout.Section>
          <UsageCard stats={stats} />
        </Layout.Section>
        <Layout.Section>
          <Card>
            <BlockStack gap="200">
              <Text as="h2" variant="headingMd">Coming next</Text>
              <Text as="p" tone="subdued">
                The AI Gift Finder, gift notes, wrap and delivery dates are being set up for your store.
                They'll appear in the menu as they become available.
              </Text>
            </BlockStack>
          </Card>
        </Layout.Section>
      </Layout>
    </Page>
  )
}
