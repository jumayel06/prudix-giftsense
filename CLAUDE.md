# Claude context — prudix-giftsense

Auto-loaded by Claude Code at the start of every session. Keep this short — it's loaded into context every time. Conventions mirror Prudix Commerce (`~/Shopify/prudix-ad-copy`).

## Always do this first

**Read `LAUNCH_TODO.md`** before starting any feature work. It's the source of truth for current priorities, what's shipped, and what's next. The file is gitignored (local-only). When work completes, **mark items `[x]`** in their section AND add a one-line entry under "✅ Recently completed". Don't delete completed items.

**Track production-deployment debt in `PROD_RELEASE_CHECKLIST.md`.** Whenever you ship something with a dev-only state, an empty config value, a secret to rotate, or a "deal with this at launch" decision, **append an actionable line** to the right section. Mark unnecessary items `[-]` rather than deleting them.

**Plans and design:** `docs/GIFTSENSE_PLAN.html` (agreed features, plans, costs) → `docs/TECHNICAL_PLAN.md` (how each feature works) → `docs/SHOPIFY_PLAYBOOK.md` (install / uninstall / billing / trial / cron / GraphQL rules copied from Commerce, with the bug behind each). **Follow the playbook exactly; don't re-derive those flows.**

**After every completed task** — implemented, tests added, full `pytest tests/` green — finish your reply with:

1. **"How to test on storefront"** — concrete steps on the dev store (`prudix-commerce-dev.myshopify.com`): which dashboard page, what to click, what to expect, what to check in DevTools / Shopify admin (metafields, orders, billing), and a SQL query for any DB writes. Say so explicitly if the change is backend-only.
2. **"Next 5 pending tasks"** — from `LAUNCH_TODO.md` open `[ ]` items, prioritized, one line each with the section name. Lead with a count of pending items per section (grep `^- \[ \]` per section header).

## Project shape

- **`app/`** — FastAPI backend
  - `app/config.py` — **single source of truth** for `PLANS` (incl. `ai_tiers`), `PLAN_DEFAULT_AI_TIER`, trial limits. Monthly billing only.
  - `app/ai_models.py` — **the only place model IDs live**: `MODELS` registry, `SLOTS` (+ gradual `rollout_pct`), `AI_TIERS` (Fast/Balanced/Premium — merchants never see model names), `resolve_model`. Swap/retire a model here only (docstring has the procedure).
  - `app/plan_guard.py` — `require_feature`, `require_generation`, trial caps, daily cost cap
  - `app/llm.py` — single source for LLM calls; returns `LLMResponse`
  - `app/routes/` — `auth`, `billing`, `webhooks`, `settings` (more per feature)
  - `app/workers/main.py` — ARQ worker + crons (uninstall / scheduled-change / trial-conversion reconcilers, purge)
  - `app/purge.py` — purges every `shop_id` table (enforced by `test_purge_completeness`)
- **`core/`** — copied from Commerce: `config.py` (env), `shopify_auth.py` (token exchange, refresh, `SCOPES`), `shopify_deps.py` (`get_current_shop` + install provisioning), `shopify_graphql.py`, `db/` (models + Alembic migrations)
- **`dashboard/`** — merchant React + Polaris app (Vite), served by FastAPI from `dashboard/dist/`
- **`extensions/`** — theme app extension, Thank-you page extension, admin print actions
- **`tests/`** — pytest: `unit/`, `integration/`, `regression/`, `scenarios/`. **Don't break it.**

## Critical patterns (from Commerce)

- **Generation refund pattern:** write `UsageLog(generations_consumed=+weight)` upfront; on success `(0, tokens)`, on failure `(-weight, 0)`. Usage = `SUM(generations_consumed)`, never `COUNT()`.
- **Billing callback trusts nothing in the URL:** tier from the Shopify subscription name, replay guard on same `charge_id`, deferral recomputed server-side.
- **Webhooks:** HMAC required on every endpoint (GDPR included); idempotency via `processed_webhooks`; stale uninstall via `X-Shopify-Triggered-At`; stale cancellation via charge-id mismatch.
- **GraphQL Admin API only** — no REST Admin calls (App Store rule 2.2.4).
- **Worker queries** use `plan_status IN ('active','trial_active')` — never `'trial'`.
- **Test mocking:** patch `app.<module>.chat` returning `LLMResponse(...)`; never patch `openai`/`anthropic`. Patch names where they're *used* (e.g. `app.workers.main.get_valid_access_token`).
- **FastAPI route ordering:** literal routes before parameterized ones (`/api/x/status` before `/api/x/{id}`).
- **Storefront calls** go through the App Proxy (`/apps/giftsense/*` → `/api/storefront/*`), verified by HMAC signature.

## Common commands

```bash
# Local dev — each in its own terminal
cloudflared tunnel run prudix-commerce-dev       # shared tunnel: giftsense-dev.prudix.app → :8001
shopify app dev --config dev                     # Shopify CLI (backend on :8001 via start_backend.sh)
.venv/bin/arq app.workers.main.WorkerSettings    # ARQ worker + crons (Redis DB /1)

.venv/bin/pytest tests/ -x --tb=short -q         # full suite (must stay green)
.venv/bin/alembic upgrade head                   # apply migrations (dev DB from .env)
cd dashboard && npm run build                    # rebuild merchant dashboard
shopify app deploy --config dev|prod             # push toml (scopes, webhooks, App Proxy)
```

## Things to be careful about

- **Two Shopify apps:** dev `client_id=2ea88886e5ce1368a9ddfdf8d83cc171` (`shopify.app.dev.toml`), prod `client_id=b538375f0b27e0640a535a12fe64c41f` (`shopify.app.prod.toml`), Partner account `jumayel006@`. Always pass `--config dev` or `--config prod`.
- **Scopes live in 3 places** — both tomls + `core/shopify_auth.py:SCOPES`. Every change forces merchants to re-authorize. Don't add scopes lightly (Level 1 customer data only — no names/emails/addresses).
- **`.env` is DEV-only** (Supabase `prudix-giftsense-dev`, local Redis DB `/1` because Commerce uses `/0`). Prod credentials live only in Railway.
- **Don't run `alembic` against unknown DBs** without confirming first.
- **Never `--no-verify`** on git hooks.
- **`BILLING_TEST_MODE`** must be false in Railway at App Store submission.

## Tone preferences

- Be concise. For exploratory questions: brief recommendation + tradeoff.
- The user prefers simple solutions ("keep it simple").
