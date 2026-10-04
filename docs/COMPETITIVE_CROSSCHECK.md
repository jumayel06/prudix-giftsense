# GiftSense vs the Shopify gift-app market (cross-check, 2026-10-04)

Source: the scrape behind the original spec (`~/PycharmProjects/Shopify/bkp/shopify_gift_full.csv`, Shopify App Store
search "gift": 2,102 results, 2,074 unique apps, listing summaries only). Counts are keyword matches on those
summaries, so they're approximate; the closest competitors' listings were read in full: Giftship, PrepMyGifts, Givy,
Giftnote, Wrapit, Pal, Wrapped, Super Gift Wraps, GP Gift Wrap & Messages, Gift-On, Giftie, Swaq, Listify, QuickGift.

**Bottom line:** the thesis holds. No app combines catalog-matched AI gift finding with notes, wrap, delivery and
messages; the most complete (Wrapit, Magical Custom Fields) cover 4 core gifting functions and no AI. The gaps are in
placement and fulfillment mechanics competitors treat as standard.

## Where GiftSense is ahead

| Feature | Market |
|---|---|
| AI gift finder matched to the store's catalog, reasons grounded in listing facts | ~9 mention a gift quiz/chatbot; all generic quiz builders, none catalog-matched |
| AI note that knows the product, recipient and occasion | Only Giftie ("AI Magic writer", generic writer's-block helper) |
| AI suggestions in registries | None of ~65 registry/wishlist apps (Listify, Swym, Gift Reggie…) |
| Arrive-by date that holds the order until its ship-by day | ~52 have a date picker; they record a date, they don't hold fulfillment |
| All of it in one app | Wrapit / Magical Custom Fields come closest (4 functions, no AI) |

## At parity

Gift wrap with photos (Pal, Wrapped, Super, GP), text notes (~100 apps), price-free gift receipt (~44), printed gift
cards (~50), voice/video + QR (~34: Giftie, Gift-On, Swaq, VideoMessageConnect), registries with purchase tracking
(Listify, Swym), analytics + attach rates (Wrapit, GP), languages (~158 mention translations).

## Gaps (competitors have, GiftSense doesn't)

| # | Gap | Who has it | Status |
|---|---|---|---|
| 1 | Gift options in the **cart drawer** and at **checkout** (Plus) | Pal, Wrapped, Super, Giftship, Gift-On (~194 mention drawers, ~64 checkout) | Drawer: **done 2026-10-04**. Checkout (Plus): later |
| 2 | **Gift bags / boxes / greeting cards** as add-ons | Super, Wrapped, PrepMyGifts, Giftship box builder (~244 mention add-ons) | **Done 2026-10-04** (wrap/bag/box; cards later) |
| 3 | **Per-product wrap rules** (which products can be wrapped) | Pal, Wrapped | **Done 2026-10-04** (`no-gift-wrap` tag) |
| 4 | **QR on any packing slip** (Shopify / 3PL), not only our print action | Swaq, VideoMessageConnect | **Done 2026-10-04** (`giftsense.messages` metafield + snippet) |
| 5 | **Ship one order to several addresses** | Giftship, PrepMyGifts, Giftnote, QuickGift | Later |
| 6 | **Deliver the note digitally** (SMS/email on delivery) | Giftnote, Giftship | Later; needs recipient contact data (beyond Level 1) |
| 7 | **Record the message after checkout** (link to buyer) | Swaq, VideoMessageConnect | Deferred (Thank-you extension, week 9) |
| 8 | Gift cards, corporate/bulk, recipient marketing (Klaviyo) | Givy, Giftnote | Out of scope by decision |

Recipient's choice: 0 apps; group gifting: 4. Deferring both stands.
