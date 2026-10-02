import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Page, Layout, Card, BlockStack, InlineStack, Text, Checkbox, Button, Banner, SkeletonBodyText, Select, TextField, Tag, List,
} from '@shopify/polaris'
import { shopifyFetch, fetchJson } from '../utils/shopifyFetch'
import { parseApiError } from '../utils/apiError'
import { showToast } from '../utils/toast'

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const HOURS = Array.from({ length: 24 }, (_, h) => ({
  value: String(h), label: new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: 'numeric' }),
}))

function pretty(iso) {
  if (!iso) return ''
  const [y, m, d] = iso.split('-').map(Number)
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
}

function form(d) {
  return { ...d, processing_days: String(d.processing_days), transit_days: String(d.transit_days),
    max_days_ahead: String(d.max_days_ahead), cutoff_hour: String(d.cutoff_hour) }
}

function payload(f) {
  return { enabled: f.enabled, processing_days: Number(f.processing_days), transit_days: Number(f.transit_days),
    ship_weekdays: [...f.ship_weekdays].sort(), blackout_dates: [...f.blackout_dates].sort(),
    max_days_ahead: Number(f.max_days_ahead), cutoff_hour: Number(f.cutoff_hour) }
}

export default function ArriveByPage() {
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [f, setF] = useState(null)
  const [newDate, setNewDate] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const load = () => fetchJson('/api/settings').then(d => { setData(d); setF(form(d.delivery)) })
  useEffect(() => { load().catch(() => setError('Could not load arrive-by settings. Please refresh.')) }, [])

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const res = await shopifyFetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ delivery: payload(f) }),
      })
      if (!res.ok) {
        let json = {}
        try { json = await res.json() } catch { /* ignore */ }
        setError(parseApiError(json).message)
        return
      }
      await load()
      showToast('Arrive-by settings saved')
    } catch {
      setError('Could not save. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  if (!data || !f) {
    return (
      <Page title="Arrive-by dates">
        {error ? <Banner tone="critical" title={error} /> : <Card><SkeletonBodyText lines={8} /></Card>}
      </Page>
    )
  }

  if (!data.features.includes('arrive_by')) {
    return (
      <Page title="Arrive-by dates" subtitle="Let shoppers choose when a gift should arrive.">
        <Card>
          <BlockStack gap="300">
            <Text as="p">Shoppers pick an arrival date for their gift, and GiftSense holds the order until it&apos;s time to ship, so birthday gifts don&apos;t arrive a week early.</Text>
            <Text as="p" tone="subdued">Available on the Growth and Pro plans.</Text>
            <InlineStack><Button variant="primary" onClick={() => navigate('/plans')}>See plans</Button></InlineStack>
          </BlockStack>
        </Card>
      </Page>
    )
  }

  const set = key => value => setF(x => ({ ...x, [key]: value }))
  const toggleDay = i => setF(x => ({
    ...x, ship_weekdays: x.ship_weekdays.includes(i) ? x.ship_weekdays.filter(d => d !== i) : [...x.ship_weekdays, i],
  }))
  const addBlackout = () => {
    if (newDate && !f.blackout_dates.includes(newDate)) setF(x => ({ ...x, blackout_dates: [...x.blackout_dates, newDate] }))
    setNewDate('')
  }
  const dirty = JSON.stringify(payload(f)) !== JSON.stringify(payload(form(data.delivery)))
  const noDays = f.ship_weekdays.length === 0
  const w = data.delivery_window

  return (
    <Page
      title="Arrive-by dates"
      subtitle="Shoppers choose when a gift should arrive. GiftSense holds the order until it's time to ship."
      primaryAction={{ content: 'Save', onAction: save, loading: saving, disabled: !dirty || noDays }}
    >
      <Layout>
        {error && <Layout.Section><Banner tone="critical" title={error} onDismiss={() => setError(null)} /></Layout.Section>}

        <Layout.AnnotatedSection
          title="Date picker"
          description="Shows “When should it arrive?” in the gift panel and on the cart page. Only dates you can make are offered, labelled as estimated."
        >
          <Card>
            <BlockStack gap="300">
              <Checkbox label="Let shoppers choose an arrival date" checked={f.enabled} onChange={set('enabled')} />
              {data.delivery.enabled && w && (
                <Text as="p" tone="subdued">
                  A shopper ordering now can pick from <b>{pretty(w.earliest)}</b> to <b>{pretty(w.latest)}</b>.
                </Text>
              )}
            </BlockStack>
          </Card>
        </Layout.AnnotatedSection>

        <Layout.AnnotatedSection
          title="Shipping times"
          description={`Used to work out the earliest date and when each order must ship. Times are in your store's timezone (${data.store_timezone}).`}
        >
          <Card>
            <BlockStack gap="400">
              <InlineStack gap="400" wrap>
                <div style={{ width: 180 }}>
                  <TextField label="Processing days" type="number" min={0} max={30} value={f.processing_days}
                    onChange={set('processing_days')} autoComplete="off" helpText="Shipping days to prepare an order. Today counts as the first." />
                </div>
                <div style={{ width: 180 }}>
                  <TextField label="Days in transit" type="number" min={0} max={30} value={f.transit_days}
                    onChange={set('transit_days')} autoComplete="off" helpText="Your usual delivery time (an estimate)." />
                </div>
                <div style={{ width: 180 }}>
                  <Select label="Order cutoff" options={HOURS} value={f.cutoff_hour} onChange={set('cutoff_hour')}
                    helpText="Orders after this count from the next day." />
                </div>
              </InlineStack>
              <BlockStack gap="200">
                <Text as="span" fontWeight="semibold">Days you ship</Text>
                <InlineStack gap="200">
                  {WEEKDAYS.map((d, i) => (
                    <Button key={d} size="slim" pressed={f.ship_weekdays.includes(i)} onClick={() => toggleDay(i)}>{d}</Button>
                  ))}
                </InlineStack>
                {noDays && <Text as="p" tone="critical">Pick at least one shipping day.</Text>}
              </BlockStack>
              <div style={{ width: 220 }}>
                <TextField label="Furthest date shoppers can pick (days ahead)" type="number" min={7} max={180}
                  value={f.max_days_ahead} onChange={set('max_days_ahead')} autoComplete="off" />
              </div>
            </BlockStack>
          </Card>
        </Layout.AnnotatedSection>

        <Layout.AnnotatedSection
          title="Days you don't ship"
          description="Holidays or closures. Orders that would ship on these days ship on the open day before."
        >
          <Card>
            <BlockStack gap="300">
              <InlineStack gap="200" blockAlign="end">
                <div style={{ width: 200 }}>
                  <TextField label="Add a date" type="date" value={newDate} onChange={setNewDate} autoComplete="off" />
                </div>
                <Button onClick={addBlackout} disabled={!newDate}>Add</Button>
              </InlineStack>
              {f.blackout_dates.length > 0 ? (
                <InlineStack gap="200">
                  {[...f.blackout_dates].sort().map(d => (
                    <Tag key={d} onRemove={() => setF(x => ({ ...x, blackout_dates: x.blackout_dates.filter(b => b !== d) }))}>{pretty(d)}</Tag>
                  ))}
                </InlineStack>
              ) : <Text as="p" tone="subdued">None yet.</Text>}
            </BlockStack>
          </Card>
        </Layout.AnnotatedSection>

        <Layout.AnnotatedSection title="What happens to orders" description="Applies to orders with an arrival date.">
          <Card>
            <List>
              <List.Item>The order is tagged with its ship-by date, e.g. <b>giftsense-ship-by-2026-12-18</b>, and the date shows on Gift orders.</List.Item>
              <List.Item>Items you fulfill yourself are put <b>on hold</b> until the ship-by day, then released automatically. Items fulfilled by a 3PL are only tagged.</List.Item>
              <List.Item>If a date can no longer be met, the order is tagged <b>giftsense-ship-asap</b> and isn&apos;t held.</List.Item>
            </List>
          </Card>
        </Layout.AnnotatedSection>
      </Layout>
    </Page>
  )
}
