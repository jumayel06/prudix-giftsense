import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Card, BlockStack, InlineStack, Text, Button, ProgressBar, Icon } from '@shopify/polaris'
import { CheckCircleIcon } from '@shopify/polaris-icons'
import { fetchJson, shopifyFetch } from '../utils/shopifyFetch'

const STEPS = {
  plan: { title: 'Choose a plan', text: 'Done: your gift finder is ready to set up.' },
  catalog: { title: 'Let the gift finder learn your catalog', text: 'Your products are analyzed automatically after you choose a plan.', cta: 'View catalog', to: '/catalog' },
  try_it: { title: 'Try the gift finder yourself', text: 'Search your own catalog the way a shopper would. Test searches are free.', cta: 'Try it', to: '/playground' },
  storefront: { title: 'Turn on the Find a gift button', text: 'Switch it on in your theme editor so shoppers can use it.', cta: 'Set up storefront', to: '/storefront' },
  first_search: { title: 'Get your first shopper search', text: 'Appears here once a shopper uses the gift finder on your store.' },
}

function Dot() {
  return <div style={{ width: 20, height: 20, borderRadius: '50%', border: '2px solid var(--p-color-border)', flex: '0 0 20px' }} />
}

// Home onboarding: steps are computed from real activity (/api/onboarding),
// so nothing needs ticking by hand. Hidden once done or dismissed.
export default function OnboardingChecklist() {
  const navigate = useNavigate()
  const [data, setData] = useState(null)

  useEffect(() => { fetchJson('/api/onboarding').then(setData).catch(() => setData(null)) }, [])

  if (!data || !data.show) return null

  async function dismiss() {
    setData(d => ({ ...d, show: false }))
    await shopifyFetch('/api/onboarding/dismiss', { method: 'POST' })
  }

  const next = data.steps.find(s => !s.done)

  return (
    <Card>
      <BlockStack gap="400">
        <InlineStack align="space-between" blockAlign="center">
          <BlockStack gap="100">
            <Text as="h2" variant="headingMd">Get your gift finder live</Text>
            <Text as="p" tone="subdued">{data.done_count} of {data.total} done</Text>
          </BlockStack>
          <Button variant="plain" onClick={dismiss}>Dismiss</Button>
        </InlineStack>
        <ProgressBar progress={Math.round((100 * data.done_count) / data.total)} size="small" tone="success" />
        <BlockStack gap="300">
          {data.steps.map(s => {
            const step = STEPS[s.id]
            const isNext = next && next.id === s.id
            return (
              <InlineStack key={s.id} gap="300" blockAlign="start" wrap={false}>
                {s.done ? <div style={{ flex: '0 0 20px' }}><Icon source={CheckCircleIcon} tone="success" /></div> : <Dot />}
                <div style={{ flex: 1 }}>
                  <BlockStack gap="100">
                    <Text as="span" fontWeight={isNext ? 'semibold' : 'regular'} tone={s.done ? 'subdued' : undefined}>{step.title}</Text>
                    {isNext && <Text as="p" tone="subdued">{step.text}</Text>}
                    {isNext && step.cta && <div><Button onClick={() => navigate(step.to)}>{step.cta}</Button></div>}
                  </BlockStack>
                </div>
              </InlineStack>
            )
          })}
        </BlockStack>
      </BlockStack>
    </Card>
  )
}
