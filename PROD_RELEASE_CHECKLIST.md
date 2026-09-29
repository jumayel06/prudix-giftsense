# Production release checklist

Everything that must be flipped, set, rotated or verified before GiftSense goes live. Same conventions as Prudix Commerce's checklist: append a line whenever something ships with a dev-only state; mark unnecessary items `[-]` instead of deleting them.

---

## 🚦 Before launch — set up the production environment

### Infrastructure
- [ ] Railway project with two services from this repo: `web` (start.sh) and `worker` (`arq app.workers.main.WorkerSettings`). Health check path `/health` set in the Railway UI. `WEB_CONCURRENCY=2`, `PORT=8000`.
- [ ] Supabase **prod** project (Pro plan, `Prudix - Prod` org): encrypted backups (a customer-data-form claim), no auto-pause. Enable `vector` (the first migration also does it). Data API **off**.
- [ ] Upstash Redis prod DB: pay-as-you-go, `rediss://`, eviction **off**, budget cap set.
- [ ] Cloudflare R2 prod bucket for voice/video (private; presigned URLs only).
- [ ] DNS: `giftsense.prudix.app` → Railway custom domain (DNS-only / grey cloud).
- [ ] Separate Sentry project `prudix-giftsense-prod`.
- [ ] Postmark: new server for GiftSense in the existing account; DKIM/Return-Path verified; bounce/complaint webhook with Basic Auth.

### Production environment variables (Railway only — never in local `.env`)
- [ ] `APP_ENV=production`
- [ ] `APP_HOST=giftsense.prudix.app` (web **and** worker). No fallback exists — empty breaks billing callbacks.
- [ ] `SHOPIFY_API_KEY=b538375f0b27e0640a535a12fe64c41f`, `SHOPIFY_API_SECRET` (prod app), `SHOPIFY_API_VERSION=2026-07`, `SHOPIFY_APP_HANDLE` (prod handle from the admin URL)
- [ ] `VITE_SHOPIFY_API_KEY=b538375f0b27e0640a535a12fe64c41f` as a Railway **build-time** variable
- [ ] `DATABASE_URL` → Supabase **transaction pooler (6543)**, `postgresql+asyncpg://…?ssl=require`
- [ ] `DIRECT_DATABASE_URL` → Supabase **session pooler (5432)** for migrations
- [ ] `REDIS_URL` → Upstash `rediss://…`
- [ ] Fresh prod-only `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` with spend caps
- [ ] Fresh `TOKEN_ENCRYPTION_KEY` (never reuse dev)
- [ ] `INTERNAL_ADMIN_USERNAME` (not `admin`) + strong `INTERNAL_ADMIN_PASSWORD`
- [ ] `SENTRY_DSN`, `SENTRY_PROJECT_URL`
- [ ] `POSTMARK_SERVER_TOKEN`, `POSTMARK_FROM_EMAIL`, `POSTMARK_WEBHOOK_USER`, `POSTMARK_WEBHOOK_PASSWORD`
- [ ] `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`
- [ ] `APP_STORE_LISTING_SLUG` left **empty** until the listing is approved
- [ ] `BILLING_TEST_MODE` — `true` only while testing prod billing; see submission gate below

### Config alignment
- [ ] Scopes identical in `shopify.app.prod.toml`, `shopify.app.dev.toml` and `core/shopify_auth.py:SCOPES`
- [ ] Webhook topics in the toml match handler branches in `app/routes/webhooks.py` 1:1
- [ ] `shopify app deploy --config prod` (registers webhooks, App Proxy, extensions)
- [ ] `/docs` and `/debug` return 404 in production

- [ ] Railway worker: confirm `kick_catalog_syncs` (every 5 min) and `reconcile_catalogs` (02:30 UTC) are running; worker needs `OPENAI_API_KEY` for embeddings
- [ ] Run `alembic upgrade head` on prod (adds `catalog_products` + `catalog_syncs`, needs `vector` extension in `extensions` schema)

- [ ] Prod `OPENAI_API_KEY` has access to `gpt-6-luna` and `gpt-6-sol` (Standard/Advanced + catalog analysis); prod `ANTHROPIC_API_KEY` for Sonnet 5 (Premium) and the eval judge; run migrations through 20260928000003 (AI tiers)
- [ ] App Store listing copy says AI tiers (Standard / Advanced / Premium), never model names, so model swaps never need a listing edit

- [ ] Privacy policy + Partner Dashboard data-sharing answers name OpenAI and Anthropic as subprocessors (product data and shopper gift answers are sent to them for AI); keep in sync with the providers in `app/ai_models.py`

## 🚀 Launch day
- [ ] Install the **prod** app (Partner Dashboard → GiftSense → select store), not a `shopify app dev` session. Confirm `shop_provisioned_via_token_exchange` in logs, a `pending` row, and the plan picker.
- [ ] Protected customer data: request **Level 1** only (orders). Filed for the **dev** app 2026-09-27 — reasons: Store management, App functionality, Analytics; no protected fields (name/email/phone/address). Survey: all Purpose = Yes; Consent = Yes (agreements), Yes (consent decisions), N/A (data sale), N/A (automated decisions); Storage = Yes, Yes. **Refile identically for the prod app.**
- [ ] Make the survey answers true before submission: update prudix.app/privacy (umbrella policy) with a GiftSense section listing the order data we process and why, the shopper recordings, and the retention periods (30 days after uninstall; recordings 90 days after delivery; sessions 90 days; drafts 30 days); confirm the Terms of Service cover GiftSense.
- [ ] Every retention period promised above has a purge cron implemented and running (media, sessions, drafts), like Commerce's `purge_old_concierge_questions`.
- [ ] Demo store: install prod, approve a test plan while `BILLING_TEST_MODE=true`, set up real gift finder content; **storefront password in the reviewer notes**.
- [ ] 🚨 **SUBMISSION GATE — `BILLING_TEST_MODE=false`** in Railway, then verify one real charge screen shows a real (non-test) charge.
- [ ] Full billing walk-through on a clean store: install → trial → convert → upgrade → downgrade (deferred) → cancel → reinstall (trial blocked) → resubscribe.
- [ ] Upgrade Postmark to a paid plan on submission day.

## 🔒 Security hardening
- [ ] Verify Shopify webhooks reject bad HMAC on a real prod-signed payload
- [ ] Sentry scrubber: no tokens, HMACs or DB/Redis URLs in events

## 📈 After first traffic
- [ ] Worker logs show daily `reconcile_uninstalled_shops_complete`, hourly `reconcile_scheduled_plan_changes_complete` and `reconcile_trial_conversions_complete`
- [ ] Watch per-merchant AI cost vs. plan margins (target ≥80% worst case)
- [ ] After approval: set `APP_STORE_LISTING_SLUG`

## 🧹 Tech debt picked up during dev
- [ ] Dockerfile builds `dashboard/` — requires the dashboard shell to exist before the first Railway build
