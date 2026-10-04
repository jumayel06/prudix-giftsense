"""Weekly GiftSense email cron (Growth+), Mondays 13:00 UTC (morning in the
Americas, afternoon in Europe). Same shape as Prudix Commerce's digest crons:
one shop's failure never stops the others, and `Shop.digest_last_sent_at`
stops a double send after a worker restart.

Who gets it: plan has `weekly_email`, status active or trial_active, the
merchant left the email on (Settings), we have the owner's email, and there
was some gift finder activity in the last two weeks (no empty reports).
"""
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select

from app.config import PLANS
from app.services import analytics, delivery, digest_email, theme_status, wrap
from app.services.postmark_client import PostmarkError, send_email
from core.db.models import Shop
from core.db.session import AsyncSessionLocal

logger = structlog.get_logger()

MIN_DAYS_BETWEEN = 5


def tips_for(shop: Shop) -> list[tuple[str, str, str]]:
    """Up to two next steps from the shop's setup, most important first."""
    features = PLANS.get(shop.plan_tier, PLANS["starter"])["features"]
    tips = []
    if theme_status.warning(shop):
        tips.append(("Turn the gift finder back on", "It's off in your live theme, so shoppers can't use it.",
                     "storefront"))
    w = wrap.wrap_settings(shop)
    if "gift_wrap" in features and not (w["enabled"] and w["ready"]):
        tips.append(("Offer gift wrap", "Shoppers add it right in the gift panel or the cart drawer. It takes a minute.",
                     "wrap"))
    if "arrive_by" in features and not delivery.delivery_settings(shop)["enabled"]:
        tips.append(("Let shoppers pick an arrival date", "Orders wait until it's time to ship, so gifts land on the day.",
                     "delivery"))
    return tips[:2]


def _due(shop: Shop, now: datetime) -> bool:
    last = shop.digest_last_sent_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last is None or now - last >= timedelta(days=MIN_DAYS_BETWEEN)


async def send_weekly_digests(ctx: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    tiers = [t for t, p in PLANS.items() if "weekly_email" in p["features"]]
    sent = skipped = failed = 0
    async with AsyncSessionLocal() as db:
        shops = (await db.execute(select(Shop).where(
            Shop.plan_tier.in_(tiers), Shop.plan_status.in_(["active", "trial_active"]),
            Shop.digest_email_opt_in.is_(True),
        ))).scalars().all()
        for shop in shops:
            await db.refresh(shop)
            domain = shop.shop_domain
            try:
                if not shop.shop_owner_email or not _due(shop, now):
                    skipped += 1
                    continue
                week = await analytics.summary(db, shop, 7, now)
                before = await analytics.summary(db, shop, 7, now - timedelta(days=7), daily=False)
                if not (week["sessions"] or week["gift_orders"] or before["sessions"] or before["gift_orders"]):
                    skipped += 1
                    continue
                subject, body, text = digest_email.render(domain.removesuffix(".myshopify.com"), domain, week, before,
                                                          tips=tips_for(shop))
                await send_email(to_email=shop.shop_owner_email, subject=subject, html_body=body, text_body=text,
                                 tag="giftsense-weekly")
                shop.digest_last_sent_at = now
                await db.commit()
                sent += 1
            except PostmarkError as e:
                failed += 1
                logger.warning("weekly_digest_send_failed", shop=domain, error=str(e)[:200])
                await db.rollback()
            except Exception as e:  # noqa: BLE001 — one shop never stops the rest
                failed += 1
                logger.exception("weekly_digest_failed", shop=domain, error=str(e)[:200])
                await db.rollback()
    logger.info("weekly_digest_complete", sent=sent, skipped=skipped, failed=failed)
    return {"sent": sent, "skipped": skipped, "failed": failed}
