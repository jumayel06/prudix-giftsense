# Shopify Playbook: How Prudix Commerce Does It

How Prudix Commerce (`~/Shopify/prudix-ad-copy`, commit `2a5c0eb`) handles install, uninstall, billing, trials, plan changes, webhooks, crons and GraphQL. GiftSense copies these patterns: each one was fixed at least once during Commerce's pre-submission work (commits `f672347`, `b0ade8e`, `c40ca79`, `addf31b`, `51a569e`, `ad253d0`, `d5e3363`). **Copy the code; don't re-derive it.**

Each rule lists the Commerce file to copy from, and marks anything GiftSense does differently.

**GiftSense is monthly billing only.** Skip Commerce's annual logic when copying: `is_annual`, `price_usd_annual`, the interval query param, `derive_is_annual_from_subscription_name`, 365-day paid periods, and the rolling 30-day anchor in `effective_cycle_start`. Keep everything else, including deferred downgrades, which still matter between monthly tiers.

---

## 1. Install (managed install + token exchange)

Source: `core/shopify_deps.py`, `core/shopify_auth.py`.

- **Shopify uses managed installation.** `/auth/callback` is never called on install. The shop row is provisioned in `get_current_shop` on the first authenticated embedded-app request. It exchanges the App Bridge session token for an offline token (`exchange_session_token_for_offline_token`) and creates the row with `plan_status='pending'`, `plan_tier='none'`.
  - *Commerce bug (2026-09-08):* provisioning only in `/auth/callback` meant a fresh install created no row, showed no plan picker and 404'd on `/api/stats`. The dev store hid it because a row already existed.
- **Tokens must be expiring:** send `expiring: 1`. Shopify's Admin API now 403s non-expiring offline tokens. The response carries `expires_in` and `refresh_token`. `get_valid_access_token` refreshes 5 minutes before expiry and persists the new pair.
- **`use_legacy_install_flow = false` in both tomls.** Setting it to `true` makes `shopify app deploy` reject declarative `[[webhooks.subscriptions]]`.
- **Concurrent first loads:** `shop_domain` is UNIQUE. On `IntegrityError`, roll back and re-read the existing row.
- **After provisioning,** a best-effort GraphQL `shop { ianaTimezone email }` stores `store_timezone` and `shop_owner_email`. Digest emails need a real owner email: `owner@<domain>` bounces.
- **The frontend routes `pending` to the plan picker.** A fresh install must never land on Home.
- **The `?shop=` fallback in `get_current_shop` works outside production only.** Allowlist `APP_ENV` in {development, test} so it fails closed. (Was open in production in Commerce until `6a0a6a7`, 2026-09-27.) Real embedded requests always carry a session token. Pinned by `tests/integration/test_shop_param_fallback.py`.
- **Legacy `/auth` + `/auth/callback`** stay as a manual, non-embedded fallback only. It includes nonce/state, HMAC and a 5-minute timestamp check. `/auth` short-circuits for active shops *after* probing the token: a 401 means a missed uninstall, so it runs the uninstall handler and continues into OAuth.
- **Session-token verification:** HS256, audience = API key, `leeway=10`, `verify_iat=False`. Expired tokens log at **info** level: the frontend `shopifyFetch` retries once with a fresh token, so these shouldn't land in Sentry.
- **Never register webhooks at runtime.** Commerce did both runtime and toml registration and got every event twice with different webhook IDs, which bypassed idempotency.

## 2. Reinstall

Source: `core/shopify_deps.py::_reactivate_shop_on_reinstall`.

- A row with `plan_status in (uninstalled, purged)` or an empty token gets reactivated. That means a fresh token, `pending`/`none`, and every billing field cleared: charge id, cycle start, trial dates, grace, `data_purge_at`, `uninstalled_at`. `installed_at` is set to now.
- **Preserve `trial_used`**, so there's no second free trial, and keep merchant preferences.

## 3. Uninstall

Source: `app/routes/webhooks.py::_handle_uninstalled`, `app/workers/main.py`, `app/purge.py`.

- Clear the tokens and billing fields, and set `plan_status='uninstalled'`, `plan_tier='none'`, `uninstalled_at=now`, `data_purge_at = now + 30 days`. Keep the row so `trial_used` survives.
- **Stale-webhook guard:** ignore the webhook if `X-Shopify-Triggered-At` is earlier than `installed_at`. If the header is missing, fall back to "installed less than 60 s ago". `authoritative=True` skips the guard; callers use it only after a confirmed live 401.
- **Safety-net cron `reconcile_uninstalled_shops`** (01:00 UTC): probe `{ shop { id } }` for active-ish shops. On a 401, wait 5 s and re-probe. If it's still 401, run the uninstall handler. Any other result does nothing.
- **Purge:** `purge_uninstalled_shops` runs at 03:00 UTC for `data_purge_at <= now`. `shop/redact` purges immediately, but **only if still uninstalled** (the merchant may have reinstalled).
- **`test_purge_completeness`** parses `app/purge.py` and fails CI if any table with a `shop_id` column isn't purged. Copy this test as-is.

## 4. Billing (Billing API, `appSubscriptionCreate`)

Source: `app/routes/billing.py`, `app/config.py`, `core/config.py`.

- **Subscription name is the source of truth:** `"<App> <Plan> <Monthly|Annual> Plan"` (GiftSense: always Monthly). `derive_tier_from_subscription_name` and `derive_is_annual_from_subscription_name` recover tier and interval from it on both the callback and the webhook. **GiftSense:** name them `GiftSense <Plan> <Interval> Plan`.
- **Test charges:** `test = settings.billing_test_mode or not settings.is_production`. Dev stores do NOT auto-test Billing API charges. `BILLING_TEST_MODE=true` on prod lets us test the real prod app; it **must be false at submission**.
- **Trial:** offered only if `not trial_used` **and** the plan is monthly (no trial on annual). Trial days come from `PLANS[tier]["trial_days"]` (7).
- **Upgrades vs downgrades:** `_should_defer_change` applies only strict upgrades (a higher `generation_limit`) immediately (`APPLY_IMMEDIATELY`). On an active plan, downgrades and same-tier or interval switches use `APPLY_ON_NEXT_BILLING_CYCLE`. This closes the "downgrade mid-cycle to reset quota" loophole. Trial and non-active shops never defer.
- **`return_url`** is built from `APP_HOST` only. **There's no fallback:** Commerce's toml-guessing fallback once sent a prod billing callback to the dev tunnel.

### `/billing/callback` (unauthenticated and replayable, so trust nothing in the URL)

1. Re-query the subscription by GID (`node(id)`: status, name, line-item interval).
2. **A null node on HTTP 200:** retry for about 3 seconds (Shopify writes new charges asynchronously). If it is still null, **do not activate**; the `app_subscriptions/update` webhook activates genuine charges. **Also refuse `PENDING`** (created but never approved; the id is visible in the confirmation URL). Only `ACTIVE` activates; the webhook applies the trial for webhook-first activations. (Fixed in both apps 2026-09-27; Commerce `6a0a6a7`.) On a non-200 response, don't activate.
3. **Derive tier and interval from the subscription name**, never from the `plan`/`interval`/`deferred` query params.
4. **Replay guard:** if this is the same `charge_id` the shop is already active or trialing on, do nothing. Otherwise a merchant could reopen the URL to reset their monthly quota.
5. **Recompute deferral server-side.** For a deferred change, record `scheduled_plan_tier` and `scheduled_change_at = cycle_start + 30 or 365 days`, and log a `change_scheduled` BillingEvent. Don't touch the tier, status or cycle start. First refresh the row: on dev stores the "next cycle" can already have been applied by the webhook.
6. **Immediate activation:** set charge id, `billing_cycle_start=now`, `is_annual`, and clear any schedule. The first eligible monthly plan becomes `trial_active` (`trial_used=True`, trial start/end); otherwise it's `active`. Update `selected_model` to the plan default if the current model isn't allowed on the new plan.
7. **Race-safe write:** use an explicit `UPDATE shops SET plan_status='active', grace_period_ends_at=NULL`. The concurrent cancellation webhook for the *old* subscription can't then win with a stale session.
8. **Declined:** only set `declined` if the shop isn't already active or trialing.
9. **Redirect** to `https://{shop}/admin/apps/{api_key}`.

## 5. `app_subscriptions/update` webhook

Source: `app/routes/webhooks.py::_handle_subscription_update`.

- Normalize the charge id with `_extract_numeric_id` (GID → number) before comparing.
- **ACTIVE:**
  - Always: re-derive tier and `is_annual` from the name, and clear `scheduled_*`.
  - `trial_active`, trial not yet ended: it's `trial_confirmed`. Keep the trial.
  - `trial_active`, trial ended: it's `trial_converted`. Set `active` and reset `billing_cycle_start`.
  - `pending`/`none` (the webhook beat the callback): `activated`.
  - Already active: it's a `renewed` cycle. If `billing_cycle_start` is less than 60 s old, it's a duplicate delivery (`activation_confirmed`).
  - Use the forced `active` UPDATE on every path that sets `active`.
- **CANCELLED:**
  - Ignore it if the charge id differs from the current one, or if the cancelled name's tier ≠ the current tier on an active shop. Both are stale echoes of a plan switch.
  - Otherwise, set grace to start **after the paid period**: `cycle_start + 30 or 365 days + 7`. Use a conditional `UPDATE … WHERE shopify_charge_id = :cancelled`; if 0 rows change, treat it as stale.
- **DECLINED / EXPIRED:** ignore them if the charge id moved on. Expired means the trial ended unpaid: grace starts now, for 7 days.
- Always log a `BillingEvent` row.

## 6. Plan access rules

Source: `app/plan_guard.py`.

- `GENERATE_STATUSES = {active, trial_active}`. `VIEW_STATUSES` adds `cancelled` and `expired`.
- **Cancelled but still inside the paid period** keeps full access, including generation. After that comes 7 days of **read-only** grace, then lockout.
- **Annual plans reset quota monthly:** `effective_cycle_start` rolls a 30-day anchor forward from `billing_cycle_start`.
- **During the trial,** the limit is `min(generation_limit, trial_generations)`, with a friendly 429 explaining when the full limit starts.
- **Usage accounting:** write `+weight` upfront, then `(0, tokens)` on success or `(-weight, 0)` on failure. Count with `SUM(generations_consumed)`, **never** `COUNT()`.
- **Per-tier `daily_cost_cap_usd`** is a backstop (429 until UTC midnight).
- **Plan-gating audit:** every route that reads or writes gated data calls `require_feature`. Commerce found 4 ungated routes in its audit.
- **Every worker query must use `plan_status IN ('active','trial_active')`.** Commerce had `'trial'` in 8 places, so trial shops were silently skipped. `test_trial_status_workers.py` pins this.

## 7. Crons (ARQ `WorkerSettings.cron_jobs`)

Copy these from Commerce as-is:

| Cron | When (UTC) | Purpose |
|---|---|---|
| `cleanup_processed_webhooks` | 00/06/12/18:00 | Drop idempotency rows older than 72 h |
| `reconcile_uninstalled_shops` | 01:00 | Missed-uninstall safety net (double-401 probe) |
| `reconcile_scheduled_plan_changes` | hourly :20 | Past-due deferred changes vs `currentAppInstallation.activeSubscriptions` |
| `purge_uninstalled_shops` | 03:00 | 30-day retention purge |
| `purge_old_usage_logs` | 04:00 | Per-plan history trim (if we keep `history_days`) |

Rules: each shop runs in its own `try/except`, so one bad shop never aborts a sweep. Log a `*_complete` line with `scanned` and `caught` counts.

**GiftSense additions:** `reconcile_trial_conversions` (Commerce listed a missing trial-conversion reconcile as tech debt on 2026-09-19; we build it from day one), `release_ship_by_holds` (hourly), `purge_orphan_media` (daily), `purge_expired_media` (daily), `rollup_analytics` (nightly), `sync_catalog_reconcile` (nightly), `weekly_digest` (Monday).

## 8. Webhooks (general)

Source: `app/routes/webhooks.py`.

- **HMAC is required on every endpoint,** including the 3 compliance endpoints: `x_shopify_hmac_sha256: str = Header(...)`. Commerce had it optional on the compliance endpoints until `ad253d0`; that was a review finding.
- **Idempotency:** `processed_webhooks` is keyed on `X-Shopify-Webhook-Id`, falling back to `sha256(topic|shop|body)` when the header is missing.
- **Toml topics must match handler branches 1:1.** Adding a topic can require a scope, e.g. `inventory_levels/update` needs `read_inventory`.
- **GiftSense differences:**
  - `customers/redact` deletes, and `customers/data_request` emails to the store owner, every row keyed by customer or order ID (`app/services/gdpr.py`, same design as Commerce `6a0a6a7`). Register each new GiftSense table in `CUSTOMER_TABLES`.

## 9. GraphQL only

Source: `core/shopify_graphql.py` (commit `d5e3363`).

- **App Store rule 2.2.4:** new public apps must use the GraphQL Admin API. Commerce migrated every REST call before review. **GiftSense makes zero REST Admin calls from day one.**
- `shopify_graphql_post(shop, token, query, variables)` handles the URL and headers. Callers check `.status_code` and `data.<mutation>.userErrors`.
- Use `product_gid` / `numeric_id_from_gid` at the boundaries.

## 10. Config, environments, deploy

Source: `core/config.py`, `core/db/session.py`, `.env.example`, `Dockerfile`, `start.sh`, `Procfile`.

- **Two Shopify apps (dev and prod),** so every `shopify app …` command needs `--config dev` or `--config prod`.
- **Keep scopes in sync in 3 places:** both tomls and `core/shopify_auth.py:SCOPES`. Any change forces re-authorization.
- **`[app_proxy]` changes need `shopify app deploy`.** `shopify app dev` doesn't register them.
- **`shopify.web.toml`: `port` and `webhooks_path` are top-level keys.** The CLI has no `[dev]` table, so Commerce's `[dev] port = 8000` is ignored and the CLI assigns a random `PORT`. Its startup self-check then posts a test `app/uninstalled` to the wrong port and the default `/api/webhooks` path, and fails. GiftSense sets `port = 8001` and `webhooks_path = "/webhooks"` at the top level, and `start_backend.sh` binds `${PORT:-8001}`. (Found 2026-09-27.)
- **Env is strict:** `APP_ENV` required, `APP_HOST` required, with no fallbacks. Local `.env` holds dev credentials only; Railway holds prod only. Never mix them.
- **`dashboard/index.html`** uses `%VITE_SHOPIFY_API_KEY%`. Never hardcode a client id.
- **Supabase:**
  - The app uses the **transaction pooler (6543)** with `NullPool`, `statement_cache_size=0`, `prepared_statement_cache_size=0` and unique statement names. This fixed prod errors `EMAXCONNSESSION` and `InvalidSQLStatementNameError`.
  - Alembic uses `DIRECT_DATABASE_URL` (5432).
  - Prod is on Pro for encrypted backups (a customer-data-form claim) and no auto-pause.
- **Upstash:** use `rediss://`, pay-as-you-go, eviction off. Free-tier databases get deleted after about 30 days idle.
- **Sentry scrubber** strips auth headers, query strings, and database/Redis URLs from exception text.
- **`is_production`** gates `/docs` and `/debug`.

## 11. Launch gates (from Commerce's release checklist)

- **Install the PROD app** (Partner Dashboard → prod app → select store), not the `shopify app dev` session.
- **Demo store:** install prod, approve a test subscription while `BILLING_TEST_MODE=true`, set up real content, and **put the storefront password in the reviewer notes.**
- **Set `BILLING_TEST_MODE=false`** before submitting, then verify one real charge screen.
- **Customer-data form + data-protection survey:** refile for GiftSense at Level 1 (no names, emails or addresses).
- **Postmark:** upgrade from Developer to Starter on submission day.
- **Full billing walk-through:** install → trial → convert → upgrade → downgrade (deferred) → cancel → reinstall (trial blocked) → resubscribe.

## 12. Tests to copy from Commerce

`tests/conftest.py`, `integration/test_token_exchange_provisioning.py`, `test_auth.py`, `test_billing.py`, `test_webhooks.py`, `test_purge_completeness.py`, `test_trial_status_workers.py`, `test_workers.py`, `regression/*` (`test_cycle_reset_on_trial_convert`, `test_generation_refund`, `test_inactive_plan_blocked`, `test_model_plan_gating`, `test_retrial_prevention`, `test_sum_not_count`), `scenarios/test_plan_lifecycle.py`, `unit/test_plan_guard.py`, `unit/test_shopify_auth.py`, `unit/test_sentry_scrubber.py`. Adapt the names and plans, and keep the assertions.
