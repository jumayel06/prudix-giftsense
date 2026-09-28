import { Page, Layout, Card, BlockStack, Text } from '@shopify/polaris'

// Contact details for now; Commerce's support form + ticket numbers
// (/api/support, support_tickets) are ported in a later week-1 task.
export default function SupportPage() {
  return (
    <Page title="Support">
      <Layout>
        <Layout.Section>
          <Card>
            <BlockStack gap="200">
              <Text as="h2" variant="headingMd">We're here to help</Text>
              <Text as="p">
                Email <Text as="span" fontWeight="semibold">support@prudix.app</Text> with your store address
                and a short description, and we'll get back to you within one business day.
              </Text>
            </BlockStack>
          </Card>
        </Layout.Section>
      </Layout>
    </Page>
  )
}
