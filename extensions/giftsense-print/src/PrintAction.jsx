/**
 * Order details → Print → "GiftSense gift cards".
 * The printable page is served by our backend (GET /print/gift-cards, see
 * app/routes/print_cards.py): one card per gift with its note. The src is a
 * relative path, so Shopify loads it from the app URL with a session token.
 */
import '@shopify/ui-extensions/preact';
import { render } from 'preact';

export default async () => {
  render(<Extension />, document.body);
};

function Extension() {
  const orderId = shopify.data.selected?.[0]?.id;
  const src = orderId ? `/print/gift-cards?orderId=${encodeURIComponent(orderId)}` : undefined;
  return (
    <s-admin-print-action src={src}>
      <s-text>Prints a gift card for each gift in this order, with its note.</s-text>
    </s-admin-print-action>
  );
}
