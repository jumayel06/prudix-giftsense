/**
 * Print menu → "GiftSense gift notes", on the order page and (bulk) the orders
 * list. The merchant picks gift note cards and/or a price-free gift receipt; the
 * page is served by our backend (GET /print/gifts, app/routes/print_cards.py).
 * The src is a relative path, so Shopify loads it from the app URL with a
 * session token.
 */
import '@shopify/ui-extensions/preact';
import { render } from 'preact';
import { useState } from 'preact/hooks';

export default async () => {
  render(<Extension />, document.body);
};

function Extension() {
  const ids = (shopify.data.selected || []).map(s => s.id).slice(0, 50);
  const [cards, setCards] = useState(true);
  const [receipt, setReceipt] = useState(false);
  const docs = [cards && 'cards', receipt && 'receipt'].filter(Boolean);
  const src = ids.length && docs.length
    ? `/print/gifts?${new URLSearchParams({ orderIds: ids.join(','), docs: docs.join(',') })}`
    : undefined;

  return (
    <s-admin-print-action src={src}>
      <s-stack gap="base">
        <s-text>{ids.length > 1 ? `Print for ${ids.length} orders:` : 'Print for this order:'}</s-text>
        <s-checkbox
          label="Gift note cards (one per gift, with its note)"
          checked={cards}
          onChange={e => setCards(e.currentTarget.checked)}
        />
        <s-checkbox
          label="Gift receipt (no prices)"
          checked={receipt}
          onChange={e => setReceipt(e.currentTarget.checked)}
        />
      </s-stack>
    </s-admin-print-action>
  );
}
