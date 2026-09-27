from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    shopify_api_key: str = ""
    shopify_api_secret: str = ""
    shopify_api_version: str = "2026-07"

    # App runtime connection. In prod this points at Supabase's TRANSACTION-mode
    # pooler (port 6543), which multiplexes many clients over few Postgres
    # backends — the only way to serve high concurrency. Used with NullPool
    # (see core/db/session.py) so Supavisor, not SQLAlchemy, owns the pooling.
    database_url: str = ""
    # Dedicated connection for Alembic migrations ONLY. Migrations need a stable,
    # session-pinned connection (transaction-mode pooling breaks DDL + the
    # boot-time `alembic upgrade head`), so this points at the SESSION-mode
    # pooler / direct connection (port 5432). Falls back to database_url when
    # unset (local dev, where there's no separate pooler).
    direct_database_url: str = ""
    # Env-driven (REDIS_URL) — no baked default; comes from Railway (prod, Upstash
    # rediss://) / .env (local redis://localhost).
    redis_url: str = ""

    openai_api_key: str = ""
    anthropic_api_key: str = ""

    token_encryption_key: str = ""
    # Public host this backend is reachable at (APP_HOST) — the single source of
    # truth for building absolute URLs: OAuth redirect_uri, billing callback,
    # admin links. Env-driven, no default (empty fails loudly rather than emit
    # the wrong host). Dev = the Cloudflare tunnel (giftsense-dev.prudix.app);
    # prod = giftsense.prudix.app.
    app_host: str = ""

    sentry_dsn: str = ""
    # REQUIRED, no default. Drives is_production (which gates /docs, /debug, prod
    # CORS, JSON logging). A missing APP_ENV must fail loudly at startup rather
    # than silently run prod as development (exposing /docs + /debug). Set in
    # Railway (production) / .env (development) / CI (test).
    app_env: str

    # Force Shopify billing charges into TEST mode even on prod (APP_ENV=production).
    # Lets us exercise the real prod app's billing flow without a real charge —
    # dev stores do NOT auto-test Billing-API charges, only the `test` field does.
    # ⚠️ MUST be false/unset before App Store submission, or real merchants are
    # never charged. See PROD_RELEASE_CHECKLIST.md launch-day gate.
    billing_test_mode: bool = False

    # Internal admin dashboard (HTTP Basic Auth at /admin/*). Env-driven — no
    # baked default (the guessable "admin" was a weak default; prod uses a
    # non-obvious username). Both come from Railway (prod) / .env (local).
    internal_admin_username: str = ""
    internal_admin_password: str = ""

    # Estimated fixed monthly infrastructure cost (Railway + Supabase + Upstash + R2).
    # Used in the admin Financials page to compute net profit. Update as plans change.
    monthly_infra_cost_usd: float = 70.0

    # Fallback per-shop daily LLM cost cap (USD), used only when a plan doesn't
    # set its own `daily_cost_cap_usd`. Protects margin from runaway scripts or
    # abuse, beyond the generation limit. Set to 0 to disable.
    max_daily_cost_usd_per_shop: float = 20.0

    # Shopify App Store listing slug. The "leave a review" banner only fires
    # when this is set — on dev/staging where the app isn't published, the
    # banner is suppressed AND the per-shop `review_prompt_shown` flag is NOT
    # flipped, so the prompt will correctly fire for real merchants once we
    # set the slug after Shopify approves the listing. The slug is the part
    # after `apps.shopify.com/` in the listing URL.
    app_store_listing_slug: str = ""

    # Sentry project URL for the "Errors" deep-link in the admin dashboard.
    # Example: https://sentry.io/organizations/prudix/issues/?project=12345
    # Leave empty to hide the link.
    sentry_project_url: str = ""

    # Postmark — powers the weekly sales email (Growth+). If
    # postmark_server_token is empty, the digest worker logs instead of sending.
    postmark_server_token: str = ""
    postmark_from_email: str = "digest@prudix.app"

    # Postmark webhook Basic Auth credentials — configured on the Postmark
    # server's Webhooks tab (Bounce + SpamComplaint events post here).
    # Postmark's recommended auth is Basic Auth on the webhook URL. When
    # both are empty (dev), the endpoint accepts unauthenticated calls so
    # local tests / curl replays work. In prod BOTH must be set — the
    # endpoint 401s any request that fails the constant-time comparison.
    postmark_webhook_user: str = ""
    postmark_webhook_password: str = ""

    # Shopify Admin app handle — the {app_handle} segment in
    # https://admin.shopify.com/store/{shop}/apps/{app_handle}/... Used to build
    # merchant-facing deep links in digest emails so clicks land inside the
    # embedded app in Shopify admin (not on our marketing domain). ENV-SPECIFIC:
    # the dev and prod apps have different handles. Env-driven, no baked
    # default — set SHOPIFY_APP_HANDLE in Railway (prod) / .env (local).
    shopify_app_handle: str = ""

    # Cloudflare R2 — voice/video message storage (S3-compatible). Empty until
    # the media feature ships; the media service refuses uploads when unset.
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def get_app_host(self) -> str:
        # Driven entirely by the APP_HOST env var (Railway in prod, .env locally).
        # No fallback/default — an empty value here is a misconfiguration that
        # should surface loudly rather than silently emit the wrong host (that's
        # how a prod billing callback pointed at the dev tunnel on 2026-09-11).
        return self.app_host


settings = Settings()
