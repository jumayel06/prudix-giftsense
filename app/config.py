"""Plans, model access, and lifecycle constants — the single source of truth.

Same structure as Prudix Commerce's app/config.py. GiftSense is MONTHLY billing
only (no annual plans). Numbers match docs/GIFTSENSE_PLAN.html; change them
here, never in core/ or the dashboard.
"""
APP_ID = "giftsense"

TRIAL_DAYS          = 7
CYCLE_DAYS          = 30
GRACE_PERIOD_DAYS   = 7
DATA_RETENTION_DAYS = 30

USAGE_WARN_THRESHOLD  = 0.75
USAGE_BLOCK_THRESHOLD = 1.00

# AI tier (app/ai_models.py: Standard / Advanced / Premium) a plan starts on:
# each plan's best option (most merchants never change it). Models chosen by
# the 2026-09-28 six-model eval: Standard (GPT-6 Luna) fastest with 97%
# faithful reasons; Advanced (GPT-6 Sol) 100%; Premium (Sonnet 5) the most
# good picks.
PLAN_DEFAULT_AI_TIER = {
    "starter": "standard",
    "growth":  "advanced",
    "pro":     "premium",
}


# Display categories for the plan picker.
FEATURE_CATEGORIES = {
    "Finding the gift":    ["gift_finder", "catalog_control", "storefront_placements", "shopper_language"],
    "Preparing the gift":  ["ai_notes", "gift_groups", "gift_wrap", "arrive_by", "gift_cards_print",
                            "gift_receipt", "voice_messages", "video_messages", "recipients_choice"],
    "Registries":          ["registries"],
    "Proving it pays off": ["basic_analytics", "full_analytics", "weekly_email"],
    "Extras":              ["hide_branding", "priority_support"],
}

FEATURE_LABELS = {
    "gift_finder":           "AI Gift Finder",
    "catalog_control":       "Catalog control and test area",
    "storefront_placements": "Storefront placements",
    "shopper_language":      "Shopper's own language",
    "ai_notes":              "AI gift notes",
    "gift_groups":           "Several gifts in one order",
    "gift_wrap":             "Gift wrap",
    "arrive_by":             "Arrive-by dates",
    "gift_cards_print":      "Printable gift cards and tags",
    "gift_receipt":          "Gift receipt (hide prices)",
    "voice_messages":        "Voice messages",
    "video_messages":        "Video messages",
    "recipients_choice":     "Recipient's choice (swap size or color)",
    "registries":            "Gift registries with AI suggestions",
    "basic_analytics":       "Basic analytics",
    "full_analytics":        "Full analytics",
    "weekly_email":          "Weekly sales email",
    "hide_branding":         "Hide the \"by GiftSense\" link",
    "priority_support":      "Priority support",
}

_STARTER_FEATURES = [
    "gift_finder", "catalog_control", "storefront_placements", "shopper_language",
    "ai_notes", "gift_groups", "gift_wrap", "gift_cards_print", "gift_receipt",
    "basic_analytics",
]
_GROWTH_FEATURES = _STARTER_FEATURES + [
    "arrive_by", "voice_messages", "full_analytics", "weekly_email", "hide_branding",
]
_PRO_FEATURES = _GROWTH_FEATURES + [
    "video_messages", "recipients_choice", "registries", "priority_support",
]

PLANS = {
    "starter": {
        "name":                        "Starter",
        "price_usd":                   19.00,
        "trial_days":                  TRIAL_DAYS,
        "generation_limit":            600,
        "trial_generations":           60,
        "ai_tiers":                    ["standard"],
        "features":                    _STARTER_FEATURES,
        "max_products":                250,
        "product_rereads_per_month":   200,
        "media_messages_per_month":    0,
        "trial_media_messages":        0,
        "daily_cost_cap_usd":          1.00,
    },
    "growth": {
        "name":                        "Growth",
        "price_usd":                   49.00,
        "trial_days":                  TRIAL_DAYS,
        "generation_limit":            1750,
        "trial_generations":           100,
        "ai_tiers":                    ["standard", "advanced"],
        "features":                    _GROWTH_FEATURES,
        "max_products":                2000,
        "product_rereads_per_month":   500,
        "media_messages_per_month":    200,
        "trial_media_messages":        5,
        "daily_cost_cap_usd":          3.00,
    },
    "pro": {
        "name":                        "Pro",
        "price_usd":                   99.00,
        "trial_days":                  TRIAL_DAYS,
        "generation_limit":            4500,
        "trial_generations":           150,
        "ai_tiers":                    ["standard", "advanced", "premium"],
        "features":                    _PRO_FEATURES,
        "max_products":                5000,
        "product_rereads_per_month":   1200,
        "media_messages_per_month":    500,
        "trial_media_messages":        10,
        "daily_cost_cap_usd":          7.00,
    },
}

# Products analyzed during the free trial, on every plan (limits the one-time
# catalog read a trial install can cost us: ~$0.30 at ~$0.003/product). On
# conversion a re-sync adds the rest up to the plan's max_products.
TRIAL_MAX_PRODUCTS = 100


def subscription_name(plan_tier: str) -> str:
    """The Shopify AppSubscription name for a plan. The tier is recovered from
    this name on both the billing callback and the app_subscriptions/update
    webhook, so it must always contain the tier key."""
    return f"GiftSense {PLANS[plan_tier]['name']} Monthly Plan"


def derive_tier_from_subscription_name(name: str | None, fallback: str = "starter") -> str:
    """Map a Shopify AppSubscription name → our plan tier.

    We name every subscription with `subscription_name`, so the tier is
    recoverable from the name alone: the authoritative signal both the billing
    callback and the `app_subscriptions/update` webhook use, instead of trusting
    caller-supplied query params. Falls back to `fallback` when no known tier
    appears.
    """
    lowered = (name or "").lower()
    return next((tier for tier in PLANS if tier in lowered), fallback)
