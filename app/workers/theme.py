"""Theme jobs (enqueued by the themes/publish webhook).

check_theme: a new live theme starts with GiftSense's app embed off and none
of its sections placed (they're saved per theme). Re-check it so the dashboard
can warn the merchant (app/services/theme_status.py), and email the store
owner once per theme the day it happens (all plans: it's an operational
alert, not the weekly report). `theme_check.emailed_for` stops repeats.
"""
import structlog
from sqlalchemy import select

from app.services import digest_email, theme_status
from app.services.postmark_client import PostmarkError, send_email
from core.db.models import Shop
from core.db.session import AsyncSessionLocal
from core.shopify_auth import get_valid_access_token

logger = structlog.get_logger()


def missing_email(shop_domain: str, theme_name: str | None, blocks_lost: bool) -> tuple[str, str, str]:
    theme = theme_name or "your new theme"
    lost = (" The Find a gift and Gift options blocks you had placed are missing from it too; add them back in the "
            "theme editor." if blocks_lost else "")
    subject = f"The gift finder is off on {theme}"
    lines = [f"You published {theme}, and the GiftSense gift finder isn't switched on in it yet, so shoppers can't "
             f"find gifts right now. App embeds are saved per theme.{lost}",
             "Open GiftSense → Storefront and use Turn on in theme editor (one click, then Save)."]
    body, text = digest_email.alert(shop_domain, title=subject, body=lines, cta_label="Open Storefront setup",
                                    cta_path="storefront", chip="Action needed")
    return subject, body, text


async def check_theme(ctx: dict, shop_domain: str) -> None:
    async with AsyncSessionLocal() as db:
        shop = (await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))).scalar_one_or_none()
        if shop is None or shop.plan_status in ("uninstalled", "purged"):
            return
        token = await get_valid_access_token(shop, db)
        theme_status.remember(shop, await theme_status.fetch_embed_status(shop_domain, token))
        await db.commit()

        warn = theme_status.warning(shop)
        check = (shop.gift_settings or {}).get("theme_check") or {}
        if not (warn and warn["theme_changed"] and shop.shop_owner_email
                and shop.plan_status in ("active", "trial_active")
                and check.get("emailed_for") != warn["theme_name"]):
            return
        subject, body, text = missing_email(shop_domain, warn["theme_name"], warn["blocks_lost"])
        try:
            await send_email(to_email=shop.shop_owner_email, subject=subject, html_body=body, text_body=text,
                             tag="giftsense-theme-missing")
        except PostmarkError as e:
            logger.warning("theme_missing_email_failed", shop=shop_domain, error=str(e)[:200])
            return
        shop.gift_settings = {**shop.gift_settings, "theme_check": {**check, "emailed_for": warn["theme_name"]}}
        await db.commit()
