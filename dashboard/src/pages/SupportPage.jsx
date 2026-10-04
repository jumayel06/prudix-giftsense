import { useState, useEffect, useCallback } from 'react'
import {
  Page, Layout, Card, Text, BlockStack, TextField, Select, Button, Banner, Badge, InlineStack, Divider,
} from '@shopify/polaris'
import { shopifyFetch, fetchJson } from '../utils/shopifyFetch'
import { parseApiError } from '../utils/apiError'

// Support requests (ported from Prudix Commerce): emailed to support@prudix.app
// and handled in the internal admin's Support page.
const CATEGORIES = [
  { label: 'Bug report', value: 'bug' },
  { label: 'Billing question', value: 'billing' },
  { label: 'Feature request', value: 'feature' },
  { label: 'Other', value: 'other' },
]
const STATUS_TONE = { open: 'attention', in_progress: 'info', resolved: 'success' }
const STATUS_LABEL = { open: 'Open', in_progress: 'In progress', resolved: 'Resolved' }

export default function SupportPage() {
  const [category, setCategory] = useState('bug')
  const [subject, setSubject] = useState('')
  const [message, setMessage] = useState('')
  const [screenshotUrl, setScreenshotUrl] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [ticketNumber, setTicketNumber] = useState(null)
  const [error, setError] = useState(null)
  const [tickets, setTickets] = useState([])

  const loadTickets = useCallback(() => {
    fetchJson('/api/support/tickets').then(setTickets).catch(() => {})
  }, [])

  useEffect(() => { loadTickets() }, [loadTickets])

  async function submit() {
    setSubmitting(true)
    setError(null)
    try {
      const res = await shopifyFetch('/api/support/ticket', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          category, subject: subject.trim(), message: message.trim(), screenshot_url: screenshotUrl.trim() || null,
        }),
      })
      let json = {}
      try { json = await res.json() } catch { /* ignore */ }
      if (!res.ok) {
        setError(res.status === 422 && screenshotUrl.trim() && !/^https?:\/\//.test(screenshotUrl.trim())
          ? 'The screenshot link should start with https://'
          : parseApiError(json).message)
        return
      }
      setTicketNumber(json.ticket_number)
      setSubject('')
      setMessage('')
      setScreenshotUrl('')
      setCategory('bug')
      loadTickets()
    } catch {
      setError('Could not send your request. Please try again, or email support@prudix.app.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Page title="Support" subtitle="Ask a question or report a problem. We reply within one business day.">
      <Layout>
        {(ticketNumber || error) && (
          <Layout.Section>
            {ticketNumber && (
              <Banner tone="success" title={`Request received: ticket #${ticketNumber}`} onDismiss={() => setTicketNumber(null)}>
                <p>We&apos;ll email you at your store&apos;s address. Mention #{ticketNumber} if you write to us.</p>
              </Banner>
            )}
            {error && <Banner tone="critical" title={error} onDismiss={() => setError(null)} />}
          </Layout.Section>
        )}
        <Layout.Section>
          <Card>
            <BlockStack gap="400">
              <Text variant="headingMd" as="h2">New request</Text>
              <Select label="Category" options={CATEGORIES} value={category} onChange={setCategory} />
              <TextField label="Subject" value={subject} onChange={setSubject} maxLength={200} autoComplete="off"
                placeholder="A short summary" />
              <TextField label="Message" value={message} onChange={setMessage} multiline={5} maxLength={5000}
                autoComplete="off" placeholder="What happened, what you expected, and the page or order it was on." />
              <TextField label="Screenshot link (optional)" value={screenshotUrl} onChange={setScreenshotUrl}
                autoComplete="off" placeholder="https://… (Loom, Google Drive, Imgur…)"
                helpText="A link to a screenshot or screen recording, if it helps." />
              <InlineStack>
                <Button variant="primary" onClick={submit} loading={submitting}
                  disabled={!subject.trim() || !message.trim()}>Send request</Button>
              </InlineStack>
              <Text as="p" tone="subdued" variant="bodySm">Or email support@prudix.app with your store address.</Text>
            </BlockStack>
          </Card>
        </Layout.Section>
        {tickets.length > 0 && (
          <Layout.Section>
            <Card>
              <BlockStack gap="300">
                <Text variant="headingMd" as="h2">Your requests</Text>
                {tickets.map((t, i) => (
                  <BlockStack key={t.id} gap="100">
                    {i > 0 && <Divider />}
                    <InlineStack align="space-between" blockAlign="center" wrap={false}>
                      <Text as="span" fontWeight="semibold">{`#${t.ticket_number} · ${t.subject}`}</Text>
                      <Badge tone={STATUS_TONE[t.status]}>{STATUS_LABEL[t.status] || t.status}</Badge>
                    </InlineStack>
                    <Text as="span" tone="subdued" variant="bodySm">
                      {`${CATEGORIES.find(c => c.value === t.category)?.label || t.category} · ${new Date(t.created_at).toLocaleDateString()}`}
                    </Text>
                  </BlockStack>
                ))}
              </BlockStack>
            </Card>
          </Layout.Section>
        )}
      </Layout>
    </Page>
  )
}
