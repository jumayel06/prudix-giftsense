# Prudix GiftSense — Technical Plan

Status: **agreed** · 2026-09-27 · merchant-facing summary: [GIFTSENSE_PLAN.html](GIFTSENSE_PLAN.html)
Inputs: `ai-gifting-app-spec.html` (product spec, 2026-09-25) + a review of `prudix-ad-copy` (Prudix Commerce, submitted to App Store review) at commit `2a5c0eb`.

This doc covers how each feature works end to end, what has to be built, and what carries over from Prudix Commerce. All 15 features ship in one release. Decisions are recorded in §13.

---

## 1. Summary of recommendations

1. **Keep the Prudix Commerce stack: FastAPI + SQLAlchemy/Alembic + ARQ + React/Polaris + a theme app extension.** The spec says "Remix app template", but every hard lesson from the first app is already built into the Python stack: managed install through token exchange, expiring offline tokens, GraphQL-only Admin calls, App Proxy HMAC, Supavisor-safe connections, billing race handling, GDPR/purge, and 1,500+ tests' worth of patterns. Switching frameworks would throw that away for no product gain.
2. **Copy `core/` and the proven app-level modules rather than extracting a shared package.** The first app designed `core/` for copy-paste. Record provenance (source commit) and turn it into a private package only if a third app arrives.
3. **Embed an LLM-written "gift profile" for each product, not the raw description.** This is the most important technical decision in the recommender. A query like "cozy · mom · birthday · $50" does not land near "Merino throw 50×60, 18.5 micron". It does land near a profile that reads "recipients: parent, partner · occasions: birthday, holiday · vibes: cozy". This approach attacks the "generic results" failure mode the spec calls out.
4. **Small catalogs skip vectors entirely.** Stores with fewer than about 60 giftable products send the whole enriched catalog to the LLM in one call. That solves the cold-start risk and gives better results on small stores.
5. **Build all shopper UI in the theme (PDP, cart, popup) plus the Thank-you page.** Checkout UI extensions on the information and shipping steps are Plus-only, so gift options cannot live there for most merchants. Data flows through cart attributes and line properties into the order, and an `orders/create` webhook turns it into order metafields and tags.
6. **Plans work like Prudix Commerce:** per-plan `models_available`, a merchant-selected model, and one pool of generations charged by `MODEL_WEIGHTS` (§8).
7. **Milestone 0 is an offline go/no-go on recommendation quality**, as the spec asks, with numeric exit criteria (§10). No Shopify plumbing gets written until the recommender beats them on fixture catalogs.

---

## 2. What we reuse from Prudix Commerce

### 2.1 Copy nearly as-is

| Source (prudix-ad-copy) | Use in GiftSense | Notes |
|---|---|---|
| `core/config.py` | Typed env settings | Drop Klaviyo/Judge.me fields; add `R2_*`, `OPENAI_EMBED_DIM` |
| `core/shopify_auth.py` | Token encryption, token exchange, refresh | New `SCOPES` (§9) |
| `core/shopify_deps.py` | `get_current_shop` + provisioning on first load | Managed install already solved here (the fix from 2026-09-08) |
| `core/shopify_graphql.py` | GraphQL POST helper | Add bulk-operation helpers |
| `core/db/session.py` | NullPool + asyncpg statement-cache workaround | Keep the whole thing, since it fixed prod incident PRUDIX-COMMERCE-PROD-5 |
| `app/llm.py` | Single LLM entry point, `LLMResponse`, `calc_cost` | Update model IDs/costs (§8). Add a structured-output path. Bump the `anthropic` SDK from 0.52 |
| `app/routes/billing.py` | `appSubscriptionCreate`, callback, deferred downgrades | Rename subscriptions "GiftSense <Plan> <Interval> Plan" so `derive_tier_from_subscription_name` keeps working. Remove the retention/inventory backfills |
| `app/routes/webhooks.py` (generic part) | HMAC, idempotency, stale detection, uninstall, subscription update, GDPR | Strip the kit/sales-leak/inventory handlers and add product and order handlers |
| `app/purge.py` pattern | 30-day post-uninstall purge | New table list; `test_purge_completeness` pattern ensures new tables are never forgotten |
| `app/plan_guard.py` | Feature gates, generation limits × model weight, daily cost cap | Same mechanics as Commerce (§8) |
| `app/admin/*` | Internal Basic-Auth admin (Jinja) | Same pages, new queries |
| `app/main.py` | Sentry scrubber, structlog, `/health`, SPA serving, admin-gated `/docs` | |
| `app/workers/main.py` | ARQ settings + generic crons | Keep webhook cleanup, uninstall reconcile, purge, and scheduled plan changes |
| `app/services/postmark_client.py`, `email_style.py` | Weekly digest (later) | Same Postmark account, new server |
| `dashboard/` shell | App Bridge, `shopifyFetch`, `PlanPickerPage`, `SettingsPage`, `SupportPage`, `OnboardingChecklist`, `UsageCard`, toast/apiError | New pages in §7 |
| Deploy: `Dockerfile`, `Procfile`, `start.sh`, `railway.json`, `shopify.web.toml` | Same Railway two-service layout | |
| `.github/workflows/tests.yml`, `.claude/hooks/pytest_passed.sh`, `.mcp.json`, `shopify-app-store-review` skill | Same TDD and CI loop | Add vitest and pgvector jobs (§10.2) |
| `tests/conftest.py` | SQLite `db_session`, `mock_db`, `make_shop`, webhook HMAC helper, settings patch | Slim `make_shop` |

### 2.2 Adapt from the AI Concierge feature

The first app's Concierge is per-product Q&A. GiftSense's is catalog-wide recommendation, but most of the plumbing is the same:

| Concierge module | GiftSense use |
|---|---|
| `services/concierge/proxy_auth.py` | Verbatim. All storefront calls go through the App Proxy (`/apps/giftsense/*`) |
| `routes/concierge_public.py::resolve_shop_from_origin` | Same two trust paths (App Proxy signature; Origin fallback in dev/tests). Plan check becomes "any paid or trial plan" |
| `services/concierge/embeddings.py` | Reuse `embed_text`; add batched `embed_texts`. Cosine search moves into Postgres (§4.4) |
| `services/concierge/moderation.py` | Verbatim, applied to optional free text and to generated notes |
| `services/concierge/budget.py` | Same atomic single-`UPDATE` gate, applied to the monthly generation pool |
| `services/concierge/cache.py` | Same dialect-aware UPSERT, keyed on a normalized intake hash plus catalog version |
| `services/concierge/product_fetcher.py` | Base for the catalog sync fetcher |
| `extensions/faq-block/assets/concierge.js` + `blocks/concierge.liquid` | Starting point for the widget: portal to `<body>`, session id, fetch helpers, CSS-variable theming, a11y dialog, mobile bottom sheet |
| Injectable `Embedder` / `Moderator` callables | Same seam pattern for tests |

### 2.3 Deliberately not copied

- **The 1,575-line `models.py`.** Write a slim model; the `Shop` row carries dozens of Commerce-only columns.
- **All Commerce feature routes, services, and workers.**

### 2.4 Infrastructure

| Resource | Plan |
|---|---|
| Shopify Partner | Same account (`jumayel006@`). Two apps, **GiftSense (Dev)** and **GiftSense**, with `shopify.app.dev.toml` / `shopify.app.prod.toml` |
| Railway | New project with `web` + `worker` services (same shape as Commerce) |
| Supabase | New dev (Free) and prod (Pro) projects, with the **`vector` extension enabled** |
| Redis | New Upstash DB. Don't share Commerce's, because ARQ queue names and cron locks would collide |
| Object storage | **Cloudflare R2** bucket for audio/video (no egress fees; Cloudflare is already in use for the tunnel) |
| Tunnel/DNS | `giftsense-dev.prudix.app` named tunnel; prod on `giftsense.prudix.app` |
| Sentry / Postmark | New Sentry projects; new Postmark server in the existing account |

---

## 3. System overview

```
 Storefront (theme app extension)                  Shopify
 ┌──────────────────────────────────┐   /apps/giftsense/*   ┌───────────┐
 │ app embed: giftsense.js launcher     │ ─── App Proxy ───▶ │  signs    │──┐
 │ block: "Find a gift" button       │   (HMAC-signed)    │  request  │  │
 │ block: PDP/cart "Gift options"    │                    └───────────┘  │
 │ cart: /cart/add.js, /cart/update  │ ── attributes/properties ──▶ Order │
 └──────────────────────────────────┘                                     │
 Thank-you page UI extension (record video link)                          │
 Admin order print action (gift card + QR)                                │
                                                                          ▼
 ┌──────────────────────── FastAPI (Railway web) ─────────────────────────────┐
 │ public: /api/public/*  (config, recommend, refine, note, media, events)    │
 │ admin:  /api/*         (session token: catalog, settings, analytics, ...)  │
 │ /webhooks  products/*, orders/create, app/*, GDPR                          │
 │ /m/{token} recipient page · /r/{token} post-purchase recorder              │
 └───────────────┬─────────────────────────────┬─────────────────────────────┘
                 │                             │
     Postgres + pgvector (Supabase)     ARQ worker (Railway) ── Redis (Upstash)
     catalog_products.embedding         catalog sync/enrich, order sync, crons
                 │
     OpenAI: embeddings + moderation · Anthropic: rerank/reasons + notes · R2: media
```

---

## 4. Feature 1: AI Gift Concierge (the core bet)

### 4.1 Catalog ingestion (worker)

**Initial sync** is triggered after install, once a plan is active:
1. Run `bulkOperationRunQuery` over `products` (status ACTIVE, published to the Online Store): id, handle, title, descriptionHtml, productType, vendor, tags, collections (titles), priceRangeV2, totalInventory/availableForSale, featuredMedia URL, onlineStoreUrl. The bulk op handles 10k+ product catalogs without pagination limits. Poll it, or subscribe to `bulk_operations/finish`.
2. Upsert into `catalog_products`, computing a `content_hash` over the fields that affect meaning.
3. Enrich each changed product (§4.2), then embed it (§4.3).
4. Record progress in `catalog_syncs`. The dashboard shows "Analyzing your catalog… 340/1,200".

**Incremental sync:** `products/create|update|delete` webhooks enqueue a per-product job. The job only re-enriches when `content_hash` changes; price and inventory changes are a cheap column update. A **nightly reconcile cron** diffs `updated_at` to recover from missed webhooks (the same pattern as Commerce's `reconcile_deleted_products`).

**Excluded automatically:** the GiftSense wrap product, gift cards, and products with `excluded=true` (the merchant toggle), $0 items, and archived or draft products.

### 4.2 Gift-profile enrichment (LLM, once per product version)

Claude Haiku 4.5 gets the product facts and returns structured JSON (structured outputs, strict schema):

```json
{
  "giftable": 0.85,                 // 0..1 — is this a plausible gift at all? (screws, refills → low)
  "recipients": ["partner","parent","friend"],   // from a fixed vocabulary
  "occasions":  ["birthday","anniversary","thank_you","holiday"],
  "vibes":      ["cozy","practical"],            // fixed vocabulary shown in the intake
  "interests":  ["reading","home"],              // open vocabulary, lowercase
  "age_band":   "adult",                          // kid | teen | adult | any
  "gift_pitch": "A heavyweight merino throw for someone who's always cold on the couch.",
  "facts":      ["100% merino wool", "50×60 in", "machine washable"]  // extracted, verbatim-grounded
}
```

- **Fixed vocabularies** for recipients, occasions, and vibes match the intake options exactly, so the tag overlap in §4.4 is exact matching rather than fuzzy.
- **Bias guard:** the prompt forbids inferring recipients from gender stereotypes unless the product copy states it. Merchants can edit any profile in the dashboard (`merchant_overrides` wins over the LLM).
- **Cost:** about 1.5k input / 250 output tokens, roughly $0.003 per product. Initial sync uses the **Message Batches API (50% off)**, so a 2,000-product store costs about $1.40 one-time. Real-time path for webhook updates.
- `facts[]` is the only source the reason-writer in §4.5 may cite, which makes grounding checkable.

### 4.3 Embedding

- **Model:** OpenAI `text-embedding-3-small` with `dimensions=512`. It's already in use in Commerce, and Anthropic has no embeddings endpoint. Reducing to 512 dimensions cuts storage and compute by 3× with negligible quality loss at our scale.
- **Embedded text:** `gift_pitch + recipients + occasions + vibes + interests + productType + title`, not the raw HTML.
- Stored in `catalog_products.embedding vector(512)` alongside `embedding_model`, so a model switch can be detected and backfilled.

### 4.4 Retrieval and ranking (no LLM, target < 150 ms)

Input: the intake `{recipient, occasion, budget_band, vibes[2–3], age_band?, free_text? ≤ 200 chars}`.

1. **Hard filters (SQL):** `shop_id`, available, not excluded, `giftable ≥ 0.4`, and **price in budget**: `price_min ≤ budget_max` and `price_max ≥ budget_min × 0.6`. Budget violations must be zero; this is an eval invariant.
2. **Vector score:** embed a synthesized query sentence ("A birthday gift for a parent who is cozy and practical, around $50" plus free text), then `ORDER BY embedding <=> :q LIMIT 60` within the shop. **An exact scan is enough.** Per-shop catalogs are ≤10k rows, so an exact cosine scan is a few milliseconds. No HNSW index is needed, which also avoids the filtered-ANN recall problem.
3. **Hybrid score:** `0.55·cosine + 0.20·recipient/occasion overlap + 0.15·vibe overlap + 0.10·giftable`. Weights live in config and are tuned by the eval in §10.
4. **Diversify:** MMR (λ≈0.7) with a penalty for the same `productType`/vendor, then keep the top 12 candidates. This directly targets the "wall of near-identical items" complaint.

**Small-catalog mode:** if the shop has fewer than ~60 eligible products, skip steps 2–4 and send every eligible profile (compact form, about 80 tokens each) to the LLM in step 4.5.

### 4.5 Rerank and reasons (one LLM call)

- **Model:** the merchant's selected model, from their plan's `models_available` (§8). Every offered model must pass the §10.3 bar; a model that fails isn't offered.
- **Prompt:** the intake plus 12 candidates (id, title, price, `facts[]`, profile). The model returns 3–5 picks as `{product_id, reason ≤ 140 chars}` via strict structured output. The system prompt is static so it can be prompt-cached; the intake and candidates go after the breakpoint.
- **Post-validation** (any failure drops that pick):
  - `product_id` must be in the candidate set (no invented products).
  - Any price or number mentioned in the reason must match the product's facts or price (regex check).
  - Length and banned-words filter.
- **Fallback:** if the LLM errors, times out after 6 s, or the shop is over budget, return the top 5 from §4.4 with **templated reasons** ("Fits your $50 budget · tagged cozy & practical"). **The widget never breaks because of an LLM or budget problem.** It degrades gracefully instead.
- **Cache:** identical normalized intake plus `catalog_version` within 24 h is served from `recommendation_cache` at zero cost.

### 4.6 "None of these"

`POST /refine` with the session id: excludes products already shown and asks **one** clarifying question. The question is chosen by the LLM from a fixed menu (e.g. "More practical or more sentimental?", "Something they can use every day, or a treat?"), and its answer maps to a vibe or interest reweighting. Then the pipeline reruns. Max 2 refinements per session. After that, show a "Browse gift collection" link instead of a dead end.

### 4.7 Latency budget

Target p95 < 4 s end to end: proxy overhead about 150 ms, query embedding about 100 ms, SQL about 20 ms, Haiku rerank 1.5–2.5 s. The widget shows **skeleton cards immediately**. (Optional later: return the vector-stage products first and stream the reasons in.)

---

## 5. Feature 2: AI-drafted gift note

### 5.1 How context carries over

- The widget creates a **session id** (`sid`, UUID in `localStorage` with a 7-day TTL). The server stores the intake and picks in `gift_sessions`.
- When a shopper adds a concierge pick to the cart, the JS calls `/cart/add.js` with line properties `_giftsense_sid` and `_giftsense_gift=1`, and `/cart/update.js` with the cart attribute `_giftsense_sid`. Underscore-prefixed keys are hidden in checkout but still arrive on the order. This is also **how conversion gets attributed without a web pixel and without customer data.**

### 5.2 Drafting

- The gift-options panel (§6) opens with the note field pre-filled from `POST /note/draft {sid, product_id}`.
- Prompt inputs: product title plus `gift_pitch`/facts, recipient, occasion, optional recipient first name (typed by the shopper and never stored beyond the session), the merchant's **tone preset** (warm & casual · minimal & elegant · playful · formal), and the merchant's banned words.
- Output: ≤ N characters (merchant setting, default 250, which matches printed-card limits). Model: the merchant's selected model; each draft (and each rewrite) uses generations by model weight.
- **Guards:** output moderation; strip URLs, emails, and phone numbers; banned-words check; retry once, then fall back to a static tone-specific template.
- **No concierge session?** The shopper came in directly through the PDP "This is a gift" checkbox. The panel then asks two chip questions (who/occasion) first. Same endpoint, a lighter context.
- **Regenerate** is limited to 3 per gift per session. Drafts are cached per (sid, gift, tone).
- **Never auto-sent:** the text lives in an editable field and only reaches the order if the shopper submits it.

### 5.3 Measuring draft acceptance (a key spec metric)

At `orders/create`, compare the final `Gift note` attribute with the stored draft: normalized edit distance of 0 means `ai_accepted`, below 0.3 means `ai_edited`, anything else means `manual`. The result is stored on `gift_orders.note_source`.

---

## 6. Gift options: gift groups, wrap, arrive-by, messages, recipient's choice, registries

### 6.1 Where the gift panel shows up

One **Gift options panel** (a modal or bottom sheet, rendered by `giftsense.js`), opened from:
- the concierge result card ("Add as a gift"),
- the **PDP app block** ("This is a gift"),
- the **cart page app block**,
- optionally, **"Is this a gift?" at checkout**, which intercepts the cart's checkout button. This is how we reach cart *drawers*, where app blocks often can't be placed.

### 6.2 Gift groups: one or several gifts per order

The panel first asks **"Where is this going?"** and the answer is stored as `delivery_mode`:
- **`direct`** (straight to the recipient): the whole cart is one gift group, with one note, one wrap and one card.
- **`self`** (to me, I'll hand them out): each cart line is assigned to a gift group labelled by recipient ("Mom", "Dad"). Each group has its own note, wrap, message and card.

A gift group is `{id, label, line_keys, note, wrap_style, message_token, note_source}`. Items added from a concierge session join that session's group automatically, because the intake already asked who it's for. Other items get a "Who's this for?" picker. We never compare shipping and billing addresses, since that would require Level 2 customer data.

The arrive-by date and gift receipt are **order-level** (one shipment).

### 6.3 Data written to the cart

| Key | Where | Visible in admin? | Purpose |
|---|---|---|---|
| `Gift for` | line property | yes, per item on the order and packing slip | group label |
| `_giftsense_gift` | line property | hidden | group id |
| `_giftsense_sid` | line property + cart attribute | hidden | attribution |
| `_giftsense_gifts` | cart attribute (compact JSON) | hidden | groups: label, note, wrap, message token |
| `Gift note` | cart attribute (direct mode) | yes | note text, readable without our webhook |
| `Arrive by` | cart attribute | yes | requested date |
| `Gift receipt` | cart attribute | yes | hide prices |
| `_giftsense_wrap_for` | property on each wrap line | hidden | links the wrap to its group |

Identical variants assigned to different people become separate cart lines (different properties), which is what we want. *Verify the cart attribute size limit early.* If a large `self` cart doesn't fit, move each group's note onto its first line's properties.

### 6.4 Wrap

- The merchant sets 2–3 styles (name, price, image). The app creates **one hidden "Gift wrap" product** (`productCreate` + `productVariantsBulkCreate`), with one variant per style, inventory untracked, and `seo.hidden=1`. *Verify whether publishing it needs `write_publications`; otherwise onboarding asks the merchant to publish it with one click.*
- One wrap line per gift group, linked by `_giftsense_wrap_for`. Cart guards: removing a group's last item removes its wrap.
- **Never pre-selected; price always shown.** App Store rule 1.1.9 requires explicit shopper consent for extra charges.

### 6.5 Arrive-by dates (Growth and Pro)

- Merchant rules: processing days, estimated transit days, shipping weekdays, blackout dates, max days ahead (default 60), daily cutoff in the shop timezone. The widget offers only achievable dates, labelled **"Arrive by (estimated)"**.
- On `orders/create`: compute `ship_by = arrive_by − transit_days`, tag the order `giftsense-ship-by-YYYY-MM-DD`, and place a **fulfillment hold** (`fulfillmentOrderHold` with a `handle`, scope `write_merchant_managed_fulfillment_orders`) on merchant-managed fulfillment orders. An hourly cron releases holds on `ship_by`.
- **3PL-fulfilled items aren't held**, only tagged, so we request just the one fulfillment permission.

### 6.6 Voice and video messages (voice: Growth, video: Pro)

Two capture paths, both ending in a QR code on the card.

**A. In the gift panel:** `MediaRecorder` in the browser. Voice up to 120 s; video (Pro) up to 60 s at 720p. Before recording, the shopper sees a consent line: "Anyone with the QR code or link can watch or listen to this." `POST /media/upload-url` returns an R2 presigned PUT (type allow-list, ≤ 30 MB, 10-minute expiry). The browser uploads directly, then `POST /media/confirm` returns a `media_token` stored on the gift group. Recordings never linked to an order are deleted after 7 days.

**B. After checkout:** a **Thank-you page checkout UI extension** (all plans) links to `/r/{order_token}`, a mobile recorder page hosted by us. `order_token` is an HMAC over (shop, order id), valid until `ship_by` or 7 days. **No GiftSense branding in this extension** (App Store rule 5.6.3).

**Delivery:** an unguessable `view_token` (128-bit) and a `segno` QR PNG in R2, both written to the gift's entry in the `giftsense.gifts` metafield. The recipient page `/m/{view_token}` is merchant-branded and `noindex`. **Retention:** deleted N days after `arrive_by` (default 90), on `customers/redact` for listed orders, and on shop purge. *Test early whether Safari plays Chrome-recorded webm; add an ffmpeg transcode job if not.*

### 6.7 Printing (admin print action)

An Admin **order print action** (plus bulk print from the order list) renders:
- **one gift card or tag per gift group**: label, note, QR, never prices (4×6 in, A6, small tag);
- a **price-free gift packing slip** when `Gift receipt` is set. This replaces asking merchants to edit their packing-slip template (App Store rule 5.1.1: no manual code edits).

### 6.8 Recipient's choice (Pro)

- The buyer ticks "Let them choose size or color" on an item. After checkout, the Thank-you extension shows a private link (`/c/{token}`) for the buyer to share. We send no emails.
- The recipient sees the product without a price and picks a variant **at the same price**. The app edits the order (`orderEditBegin` → `orderEditAddVariant` + `orderEditSetQuantity 0` → `orderEditCommit`, scope `write_order_edits`), then releases our fulfillment hold.
- Deadline: default 5 days, after which the buyer's pick ships. Only unfulfilled items can be edited.
- **Only offered when the presentment currency equals the shop currency**, because Shopify can't edit orders in other currencies. The option is hidden for those shoppers.

### 6.9 Gift registries with AI suggestions (Pro)

- The shopper must be logged in. The App Proxy passes `logged_in_customer_id`, and **that ID is the only customer data we store** (Level 1).
- "Add to registry" on PDPs. A registry page is served through the App Proxy (Liquid response) with wanted/bought status. Purchases are tracked via a `_giftsense_registry` line property on `orders/create`.
- Guests buying from a registry get the gift panel automatically. **There's no "ship to owner" address**: guests enter the address in Shopify checkout.
- AI suggestions for the owner reuse the §4 pipeline, seeded from the registry's items. Each refresh uses generations like a gift search, and results are cached per registry per day.

## 7. Merchant dashboard (React + Polaris, same shell as Commerce)

| Page | Contents |
|---|---|
| **Home / Onboarding** | Checklist: enable the app embed (deep link into the theme editor), catalog analysis progress, wrap setup, place blocks, run a test gift. Headline KPIs |
| **Catalog** | Sync status; product table with giftable score and profile tags; include/exclude toggle; **edit profile** (overrides); "Re-analyze" |
| **Concierge** | Intake options (recipients/occasions/vibes on/off, custom labels), budget bands (in shop currency), launcher appearance, **Playground**: run an intake and see picks plus reasons exactly as shoppers would. This builds trust and is great for reviewers |
| **Notes & tone** | Tone preset, sample preview, banned words, max length, message formats on/off |
| **Wrap & delivery** | Wrap styles CRUD, delivery rules |
| **Registries** (Pro) | List of registries, owner-hidden items, purchase status |
| **Analytics** | §7.1 |
| **Plans / Settings / Support** | Reused |

### 7.1 Analytics (all computed from our own tables, no web pixel needed)

Events are sent from `giftsense.js` via a batched `POST /events` beacon: `widget_open`, `intake_start`, `intake_complete`, `pick_view`, `pick_click`, `pick_atc`, `refine`, `panel_open`, `note_drafted`, `panel_submit`. Raw events are kept 90 days; a nightly rollup writes `analytics_daily`.

Metrics shown: concierge sessions, **completion rate**, **concierge→order conversion** (sessions with an order carrying the same `sid`), attributed revenue, **note acceptance rate**, wrap/note/video **attach rate** on gift orders, top occasions and budgets, and "gift orders vs. all orders" (from an `orders/create` count). That's enough to show the app paying for itself, which is what the spec requires.

---

## 8. Plans, AI models, and metering (same mechanics as Prudix Commerce)

### 8.1 Plans (`app/config.py` `PLANS`)

| | Starter | Growth | Pro |
|---|---|---|---|
| Price (monthly) | $19 | $49 | $99 |
| `features` | finder, catalog, placements, language, notes, gift groups, wrap, cards, gift receipt, basic analytics | + arrive-by, voice, full analytics + weekly email, hide badge | + video, recipient's choice, registries, priority support |
| `models_available` | gpt-4o-mini, claude-haiku-4-5 | + gpt-4.1 | + claude-sonnet-5 |
| `PLAN_DEFAULT_MODELS` | claude-haiku-4-5 | gpt-4.1 | claude-sonnet-5 |
| `generation_limit` / month | 600 | 1,750 | 4,500 |
| `trial_generations` (7 days) | 60 | 100 | 150 |
| Products in the finder | 250 | 2,000 | 5,000 |
| Product re-reads / month | 200 | 500 | 1,200 |
| Voice/video messages / month | – | 200 voice | 500 voice or video |

Prices can be revised later without code changes. The trial is 7 days, once per store (`trial_used`, as in Commerce).

### 8.2 Model weights and worst-case cost

`MODEL_WEIGHTS`: gpt-4o-mini **1**, claude-haiku-4-5 **2**, gpt-4.1 **4**, claude-sonnet-5 **4**. (Sonnet 5 is $2/$10 per MTok, cheaper than the Sonnet 4.6 that Commerce weights at 6.)

| Model | Gift search (2,500 in / 400 out) | Note (800 / 200) | $ per generation (worst) |
|---|---|---|---|
| gpt-4o-mini ($0.15/$0.60) | $0.0006 | $0.0002 | $0.0006 |
| claude-haiku-4-5 ($1/$5) | $0.0045 | $0.0020 | $0.00225 |
| gpt-4.1 ($2/$8) | $0.0082 | $0.0032 | $0.00205 |
| claude-sonnet-5 ($2/$10) | $0.0090 | $0.0036 | $0.00225 |

The weights cap cost at **$0.00225 per generation** whatever model the merchant picks. Catalog enrichment doesn't use generations: it always runs on Haiku 4.5 through the Batch API ($0.0015/product), capped by the plan's product and re-read limits.

**Worst-case monthly cost at 100% of every limit** (hosting share $1.50 / $2.00 / $2.50):
- Starter: $1.35 generations + $0.30 re-reads + $1.50 = **$3.15, a 83.4% margin**.
- Growth: $3.94 + $0.75 + $0.10 voice + $2.00 = **$6.79, a 86.1% margin**.
- Pro: $10.13 + $1.80 + $1.00 video + $2.50 = **$15.43, a 84.4% margin**.

In the first month, the one-time catalog read at the product limit lowers these to 81% / 80% / 77%. With 15% Shopify revenue share (after the first $1M) they're about 69–71%. Token counts are estimates; M0 logs real `usage`, and `MODEL_WEIGHTS` and limits get retuned if needed.

### 8.3 Metering

- `usage_logs` rows use the Commerce refund pattern: write `+weight` upfront, then `(0, tokens)` on success or `(-weight, 0)` on failure. Use SUM, not COUNT.
- The storefront gate is an atomic single `UPDATE … WHERE used + :w <= limit RETURNING` (Commerce's concierge budget pattern), so concurrent shoppers can't overspend.
- **Over the limit:** templated reasons and note templates, never a hidden widget. Per-shop `daily_cost_cap_usd` stays as a backstop.
- **Abuse limits** (Redis token bucket): gift searches 10/h per sid and 30/h per IP hash per shop; note drafts 5 per gift per sid; media uploads 3 per sid per day.

## 9. Shopify integration details

**Scopes (keep minimal; every change forces re-auth):**
`read_products, write_products` (catalog, wrap product) · `read_orders, write_orders` (order metafields and tags) · `write_order_edits` (recipient's choice) · `write_merchant_managed_fulfillment_orders` (arrive-by holds) · `read_themes` (detect whether the app embed is enabled, same as Commerce). *Maybe* `write_publications` (wrap product). **No customer scopes.** None of these is on the App Store's restricted-scope list (3.2.x), checked 2026-09-27.

**Protected customer data:** subscribing to `orders/create` requires PCD **Level 1** approval (Commerce already went through this). We **never read or store** customer name, email, phone, or address, which keeps us out of Level 2. Registries store only `logged_in_customer_id`. Level 1 duties: data minimization, merchant disclosure, retention periods, encryption at rest and in transit.

**Webhooks:** `app/uninstalled`, `app_subscriptions/update`, `products/create`, `products/update`, `products/delete`, `orders/create`, `bulk_operations/finish`, plus the 3 compliance topics. **`customers/redact` must actually handle `orders_to_redact`** by deleting `gift_orders`, `gift_media`, and note drafts for those orders. Commerce never needed this; GiftSense does.

**Order metafields** (created at install through `metafieldDefinitionCreate`, pinned so they appear on the order page): `giftsense.gifts` (JSON list of gift groups: label, note, wrap, message URL, QR URL), `giftsense.arrive_by` (date), `giftsense.ship_by` (date), `giftsense.delivery_mode`.

**Order tags:** `giftsense-gift`, `giftsense-multi-gift`, `giftsense-wrap`, `giftsense-gift-receipt`, `giftsense-scheduled`, `giftsense-ship-by-YYYY-MM-DD`, `giftsense-voice`/`giftsense-video`, `giftsense-choice-pending`, `giftsense-registry`.

**Extensions:**
| Extension | Type | Purpose |
|---|---|---|
| `giftsense-theme` | Theme app extension | app embed (`giftsense.js` + launcher), blocks: `gift-finder-button`, `gift-options` (PDP/cart) |
| `giftsense-thank-you` | Checkout UI extension (`purchase.thank-you.block.render`) | record-after-checkout link; recipient's-choice link. Unbranded |
| `giftsense-print` | Admin print actions (order details + order list bulk) | gift cards/tags per group; price-free gift packing slip |

**App Proxy:** `subpath = "giftsense"` → `https://giftsense.prudix.app/api/public`.

**Storefront performance (Built for Shopify: at most a 10-point Lighthouse drop):** the app embed loads a **<10 KB bootstrap**. The full widget JS and CSS load only on the first interaction or when a block is visible. Measure Lighthouse before and after on Dawn.

**Multi-currency:** budget bands are defined in shop currency and converted with `Shopify.currency.rate` for display. The server always filters in shop currency. The reason language follows `request.locale` (English-first; widget strings in `locales/`).

**Theme QA matrix:** Dawn, Horizon, Refresh/Sense, and two popular paid themes, each tested with a cart page and a cart drawer.

---

## 10. Test-driven development strategy

### 10.1 Same discipline as Prudix Commerce

- pytest with `asyncio_mode=auto`; directories `unit/`, `integration/`, `regression/`, `scenarios/`.
- In-memory SQLite `db_session`, `mock_db`, `make_shop`, `make_webhook_headers`. **No real external calls in tests.**
- Patch point for LLMs is `app.<module>.chat` returning `LLMResponse`. Embedders, moderators, the storage client, the Shopify GraphQL client, and the clock (`now=` params) are injectable.
- Write the test first for every service function. The full suite must stay green; the CI workflow and the `pytest_passed` hook carry over.
- `LAUNCH_TODO.md` (gitignored) and `PROD_RELEASE_CHECKLIST.md` follow the same conventions, plus a `CLAUDE.md` adapted from Commerce's.

### 10.2 New test infrastructure GiftSense needs

| Need | Approach |
|---|---|
| Deterministic recommender tests | `tests/fixtures/catalogs/*.json`: 5 catalogs (candle shop ~25, jewelry ~120, kids/toys ~300, coffee & kitchen ~500, general store ~1,500). A **fake embedder** (hashed bag-of-words → 512-d unit vector) makes ranking reproducible without OpenAI |
| pgvector SQL | Vector search sits behind `CatalogIndex.search()`. The SQLite implementation does cosine in Python; the Postgres implementation uses `<=>`. `@pytest.mark.pg` tests run in CI against a `pgvector/pgvector:pg16` service container |
| Webhook parsing | Real sample payloads (`orders/create` with `note_attributes`, line `properties`, a wrap line) as JSON fixtures |
| Storefront JS | **vitest + jsdom** for widget modules: intake state machine, cart API calls (mocked `fetch`), wrap/gift line linkage, date rules, currency conversion. A small **esbuild** step bundles the modules into `extensions/giftsense-theme/assets/giftsense.js` (Commerce's widget had no JS tests, but GiftSense's storefront logic is too important to leave untested) |
| Invariants as regression tests | 0 budget violations, 0 non-candidate product ids, fallback path when the LLM raises or times out, atomic cap under concurrency, purge completeness for every new table |

### 10.3 Offline evaluation harness (Milestone 0 go/no-go; not in CI because it costs money)

`evals/recs/`: ~50 intake personas × 5 catalogs, each with a **human-labelled set of acceptable products**. `scripts/eval_recs.py` runs the real pipeline (real embeddings and LLM) and reports:

| Metric | Exit criterion for Milestone 0 |
|---|---|
| Relevant picks in the top 5 (precision@5) | **≥ 3.5 / 5 on average** (the best tools reviewed reach ~3/5) |
| Budget violations | **0** |
| Invented / non-candidate products | **0** |
| Reason faithfulness (regex check + LLM-judge spot check) | ≥ 95% |
| Diversity (distinct product types in top 5, catalogs with ≥ 5 types) | ≥ 3 |
| p95 latency (pipeline only) | < 3.5 s |
| Cost per gift search on each offered model | within §8.2 estimates (+20%) |

The same harness runs every model in §8.2 (a model that misses the bar isn't offered), tunes the hybrid weights, and sets the small-catalog threshold. A companion **note eval** scores drafts against a rubric (mentions the product and occasion, matches the tone, within length, no clichés/URLs).

**If Milestone 0 misses the bar,** iterate on enrichment and the ranking weights first. If it still misses, rethink the thesis before building Shopify plumbing, as the spec says.

---

## 11. Data model (new tables)

| Table | Key columns |
|---|---|
| `shops` | slim copy: domain, tokens, plan/billing fields, timezone, owner email, `installed_at/uninstalled_at/data_purge_at` |
| `usage_logs`, `processed_webhooks`, `billing_events`, `jobs` | as in Commerce |
| `giftsense_settings` (1:1 shop) | tone, banned_words, intake config (JSON), budget bands, delivery rules, feature toggles, selected model, media counters, catalog_version |
| `catalog_products` | shop_id, product_id, handle, title, product_type, vendor, tags, price_min/max, available, status, image_url, url, content_hash, `gift_profile` JSON, `merchant_overrides` JSON, excluded, `embedding vector(512)`, embedding_model, enriched_at · UNIQUE(shop_id, product_id) |
| `catalog_syncs` | shop_id, kind (initial/reconcile), bulk_op_id, status, counts, timestamps |
| `recommendation_cache` | shop_id, intake_hash, catalog_version, result JSON, expires_at |
| `gift_sessions` | sid, shop_id, intake JSON, locale, currency, shown_ids, picks JSON, refinements, model, cost, created_at, converted_order_id, order_value |
| `note_drafts` | sid, gift_group_id, tone, text, regen_count, created_at (30-day retention) |
| `wrap_styles` | shop_id, name, price, image_url, variant_gid, active, position |
| `gift_media` | shop_id, token, view_token, sid, order_id, kind, storage_key, mime, bytes, duration_s, status, expires_at, view_count |
| `gift_orders` (analytics fact) | shop_id, order_id, order_name, sid, delivery_mode, gift_count, gifts JSON (per group: wrap, note_source, message_type), arrive_by, ship_by, hold_status, gift_receipt, total, currency, created_at |
| `choice_requests` | shop_id, order_id, line_item_id, token, allowed_variant_ids, chosen_variant_id, deadline, status |
| `registries` / `registry_items` | owner `customer_id` (only), shop_id, title, occasion, event_date, share_token / product_id, variant_id, wanted_qty, bought_qty |
| `analytics_events` / `analytics_daily` | raw events (90 d) / nightly rollup |

**Retention:** intake 90 days; drafts 30 days; media per setting; everything is purged 30 days after uninstall (same purge worker).

---

## 12. Milestones (about 10 weeks, one developer with Claude)

| Week | Deliverable | Exit |
|---|---|---|
| 1 | Copy the foundation from Commerce: repo scaffold, CLAUDE.md, LAUNCH_TODO, checklist, CI, hooks, install/billing/webhooks/GDPR/purge, admin shell. Start the recommendation pipeline | Suite green in CI |
| 2 | Pipeline running **offline** on fixture catalogs; eval harness across all four models | §10.3 met → **go/no-go** |
| 3 | Bulk catalog sync, Catalog page + Playground, storefront finder (embed + blocks), App Proxy, generation metering + fallbacks | Shopper finds a gift on Dawn and Horizon |
| 4 | Gift panel, gift groups, AI notes, `orders/create` → metafields/tags/`gift_orders`, print actions | Multi-gift order prints one card per group |
| 5 | Wrap, arrive-by + holds, gift receipt slip, basic analytics | Hold placed and released on schedule |
| 6 | Voice/video: recorder, R2, QR, recipient page, Thank-you extension | End-to-end video gift on iOS Safari and Chrome Android |
| 7 | Recipient's choice | Order edited to the chosen variant before shipping |
| 8 | Registries + AI suggestions | Guest purchase marks item bought |
| 9 | Full analytics, weekly email, languages, Lighthouse pass, 5-theme QA | ≤ 10-point Lighthouse drop |
| 10 | App Store readiness: review self-check, listing, demo store (storefront password in reviewer notes), screencast, submit | Submitted |

---

## 13. Decisions (all agreed 2026-09-27)

1. **Stack:** FastAPI + React/Polaris, copied from Prudix Commerce. Not Remix.
2. **Name:** GiftSense (repo `prudix-giftsense`); handle, metafield namespace and App Proxy subpath are `giftsense`. Listing title still to pick at submission.
3. **Scope:** 15 features in one release. Corporate/bulk and group gifting are dropped (small demand, large build, need Level 2 data). Registries have no ship-to-owner address.
4. **Plans:** $19 / $49 / $99 (revisable later), with Commerce-style model access and generations (§8). 7-day trial with per-plan trial generations; one trial per store.
5. **Gift groups:** `direct` or `self` delivery mode (§6.2).
6. **Media:** voice on Growth, video on Pro, stored in Cloudflare R2.
7. **Review-driven rules:** Level 1 customer data only; wrap never pre-selected; unbranded Thank-you extension; gift receipt via our print action (no template edits); recipient's choice only for shop-currency orders; no fulfillment holds for 3PL items.

## 14. Items to verify early (spikes in weeks 1–3)

- Whether publishing the hidden wrap product needs `write_publications`, and how `seo.hidden` behaves across themes.
- The cart attribute size limit for `_giftsense_gifts` on large `self` carts.
- Exact extension targets for the order-details and order-list print actions.
- Whether Safari plays Chrome-recorded webm; decide whether an ffmpeg transcode is needed.
- Structured outputs on each offered model through the SDK versions we pin (Commerce pins `anthropic==0.52.0` and `openai==1.77.0`; bump both).
- Order editing behavior on orders with our fulfillment hold in place.
