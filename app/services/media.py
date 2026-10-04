"""Voice and video messages (docs/TECHNICAL_PLAN.md §6.6): voice on Growth,
video on Pro, files in a private Cloudflare R2 bucket.

Flow: the gift panel asks for an upload URL (`issue_upload`: plan, monthly
quota, type/size/length checks, a `gift_media` row in status "pending") and
PUTs the recording straight to R2 with it (presigned, 10-minute expiry). Then
`confirm_upload` checks the object really is in R2 and within the size limit
("uploaded"). The returned `token` goes into the cart; orders/create links it
to the order ("linked", `expires_at` = delivery + MEDIA_RETENTION_DAYS).

Purges (`purge_expired`, daily cron): unlinked recordings after
ORPHAN_DAYS, linked ones at `expires_at`. Shop purge and customers/redact
delete the R2 objects too (`delete_shop_objects`, `delete_media`).

R2 is S3-compatible; boto3 does the signing. Its calls are blocking, so the
network ones run in a thread. Nothing here is customer data: the row holds
the widget session id, never who recorded it.
"""
import asyncio
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PLANS
from app.plan_guard import effective_cycle_start, may_generate
from core.config import settings
from core.db.models import GiftMedia, Shop

logger = structlog.get_logger()

KINDS = {
    # kind: (plan feature, max seconds, allowed MIME types)
    "voice": ("voice_messages", 120, {"audio/webm", "audio/mp4", "audio/ogg", "audio/mpeg"}),
    "video": ("video_messages", 60, {"video/webm", "video/mp4"}),
}
EXTENSIONS = {"audio/webm": "webm", "audio/mp4": "m4a", "audio/ogg": "ogg", "audio/mpeg": "mp3",
              "video/webm": "webm", "video/mp4": "mp4"}
MAX_BYTES = 30 * 1024 * 1024
UPLOAD_URL_TTL_SECS = 600
VIEW_URL_TTL_SECS = 6 * 3600   # presigned GET behind the recipient page
ORPHAN_DAYS = 7
MEDIA_RETENTION_DAYS = 90       # after the arrive-by date (or the order date)
COUNTED = ("uploaded", "linked")


class MediaError(Exception):
    """Shown to the shopper as-is (never mentions plans or limits)."""


@dataclass
class UploadTicket:
    token: str
    upload_url: str
    headers: dict


def configured() -> bool:
    return all((settings.r2_account_id, settings.r2_access_key_id, settings.r2_secret_access_key, settings.r2_bucket))


@lru_cache(maxsize=1)
def _client():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def enabled_kinds(shop: Shop) -> list[str]:
    """What the gift panel may offer this shop right now."""
    if not configured() or not may_generate(shop):
        return []
    features = PLANS.get(shop.plan_tier, PLANS["starter"])["features"]
    return [k for k, (feature, _, _) in KINDS.items() if feature in features]


def monthly_limit(shop: Shop) -> int:
    plan = PLANS.get(shop.plan_tier, PLANS["starter"])
    if shop.plan_status == "trial_active":
        return min(plan["media_messages_per_month"], plan["trial_media_messages"])
    return plan["media_messages_per_month"]


async def used_this_cycle(db: AsyncSession, shop: Shop) -> int:
    query = select(func.count(GiftMedia.id)).where(GiftMedia.shop_id == shop.id, GiftMedia.status.in_(COUNTED))
    floor = effective_cycle_start(shop)
    if floor:
        query = query.where(GiftMedia.created_at >= floor)
    return int((await db.execute(query)).scalar() or 0)


async def issue_upload(db: AsyncSession, shop: Shop, sid: uuid.UUID, kind: str, mime: str, size: int,
                       duration_s: int) -> UploadTicket:
    if kind not in enabled_kinds(shop):
        raise MediaError("Messages aren't available right now.")
    _, max_secs, mimes = KINDS[kind]
    base_mime = mime.split(";")[0].strip().lower()
    if base_mime not in mimes:
        raise MediaError("This browser recorded a format we can't use. Please try another browser.")
    if not 0 < size <= MAX_BYTES:
        raise MediaError("This recording is too large. Please record a shorter one.")
    if not 0 < duration_s <= max_secs + 2:          # small slack for timer rounding
        raise MediaError(f"Recordings can be up to {max_secs} seconds.")
    if await used_this_cycle(db, shop) >= monthly_limit(shop):
        raise MediaError("Messages aren't available right now.")

    token = secrets.token_urlsafe(16)
    key = f"{shop.id}/{token}.{EXTENSIONS[base_mime]}"
    db.add(GiftMedia(shop_id=shop.id, token=token, view_token=secrets.token_urlsafe(16), sid=sid, kind=kind,
                     storage_key=key, mime=base_mime, bytes=size, duration_s=duration_s, status="pending"))
    await db.commit()
    url = _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.r2_bucket, "Key": key, "ContentType": base_mime},
        ExpiresIn=UPLOAD_URL_TTL_SECS,
    )
    return UploadTicket(token=token, upload_url=url, headers={"Content-Type": base_mime})


async def _head(key: str) -> dict | None:
    from botocore.exceptions import ClientError
    try:
        return await asyncio.to_thread(_client().head_object, Bucket=settings.r2_bucket, Key=key)
    except ClientError:
        return None


async def confirm_upload(db: AsyncSession, shop: Shop, sid: uuid.UUID, token: str) -> GiftMedia:
    row = (await db.execute(select(GiftMedia).where(
        GiftMedia.shop_id == shop.id, GiftMedia.token == token, GiftMedia.sid == sid))).scalar_one_or_none()
    if row is None:
        raise MediaError("We couldn't find that recording. Please record it again.")
    if row.status != "pending":
        return row
    head = await _head(row.storage_key)
    if head is None:
        raise MediaError("The upload didn't finish. Please try again.")
    size = int(head.get("ContentLength") or 0)
    if not 0 < size <= MAX_BYTES:
        await delete_objects([row.storage_key])
        await db.delete(row)
        await db.commit()
        raise MediaError("This recording is too large. Please record a shorter one.")
    row.bytes, row.status, row.uploaded_at = size, "uploaded", datetime.now(timezone.utc)
    await db.commit()
    return row


def retention_end(arrive_by: str | date | None, now: datetime | None = None) -> datetime:
    start = now or datetime.now(timezone.utc)
    if arrive_by:
        try:
            day = arrive_by if isinstance(arrive_by, date) else date.fromisoformat(str(arrive_by))
            start = max(start, datetime.combine(day, time(), tzinfo=timezone.utc))
        except ValueError:
            pass
    return start + timedelta(days=MEDIA_RETENTION_DAYS)


@dataclass
class Message:
    view_token: str
    kind: str


def view_url(host: str, view_token: str) -> str:
    """The recipient page, on the store's own domain through the App Proxy."""
    return f"https://{host}/apps/giftsense/m/{view_token}"


def qr_svg(url: str) -> str:
    import segno
    return segno.make(url, error="m").svg_inline(scale=3, border=2, title="Scan to open the message")


async def messages_for(db: AsyncSession, shop_id: uuid.UUID, tokens: list[str]) -> dict[str, Message]:
    """{cart token: Message} for this shop's confirmed recordings."""
    tokens = [t for t in dict.fromkeys(tokens) if t]
    if not tokens:
        return {}
    rows = (await db.execute(select(GiftMedia).where(
        GiftMedia.shop_id == shop_id, GiftMedia.token.in_(tokens), GiftMedia.status.in_(COUNTED),
    ))).scalars().all()
    return {r.token: Message(r.view_token, r.kind) for r in rows}


async def for_viewing(db: AsyncSession, shop_id: uuid.UUID, view_token: str) -> GiftMedia | None:
    """A linked, unexpired recording (the recipient page). Counts the view."""
    row = (await db.execute(select(GiftMedia).where(
        GiftMedia.shop_id == shop_id, GiftMedia.view_token == view_token, GiftMedia.status == "linked",
    ))).scalar_one_or_none()
    if row is None:
        return None
    expires = row.expires_at if row.expires_at is None or row.expires_at.tzinfo else \
        row.expires_at.replace(tzinfo=timezone.utc)
    if expires is not None and expires < datetime.now(timezone.utc):
        return None
    row.view_count = (row.view_count or 0) + 1
    await db.commit()
    return row


def playback_url(row: GiftMedia) -> str:
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.r2_bucket, "Key": row.storage_key, "ResponseContentType": row.mime},
        ExpiresIn=VIEW_URL_TTL_SECS,
    )


async def link_to_order(db: AsyncSession, shop_id: uuid.UUID, tokens: list[str], order_id: str,
                        arrive_by: str | date | None) -> dict[str, Message]:
    """Attach the order's message tokens to their recordings (orders/create).
    Only confirmed uploads of this shop count; a token can't move orders.
    Returns {cart token: Message} for what was linked."""
    tokens = [t for t in dict.fromkeys(tokens) if t]
    if not tokens:
        return {}
    rows = (await db.execute(select(GiftMedia).where(
        GiftMedia.shop_id == shop_id, GiftMedia.token.in_(tokens), GiftMedia.status == "uploaded",
    ))).scalars().all()
    expires = retention_end(arrive_by)
    for row in rows:
        row.order_id, row.status, row.expires_at = order_id, "linked", expires
    return {r.token: Message(r.view_token, r.kind) for r in rows}


async def delete_objects(keys: list[str]) -> None:
    """Delete R2 objects (1,000 per request). Raises on failure, so callers
    retry instead of dropping rows whose files still exist."""
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i + 1000]
        if chunk:
            await asyncio.to_thread(_client().delete_objects, Bucket=settings.r2_bucket,
                                    Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})


async def delete_media(db: AsyncSession, rows: list[GiftMedia]) -> int:
    """Files first, then rows (a failed R2 call keeps the rows for the next try)."""
    if not rows:
        return 0
    if configured():
        await delete_objects([r.storage_key for r in rows])
    for r in rows:
        await db.delete(r)
    return len(rows)


async def delete_shop_objects(shop_id: uuid.UUID) -> None:
    """Every object under the shop's prefix, including uploads that never
    got a confirmed row (shop purge)."""
    if not configured():
        return
    client = _client()
    paginator = client.get_paginator("list_objects_v2")

    def _keys():
        out = []
        for page in paginator.paginate(Bucket=settings.r2_bucket, Prefix=f"{shop_id}/"):
            out += [o["Key"] for o in page.get("Contents") or []]
        return out
    await delete_objects(await asyncio.to_thread(_keys))


async def purge_expired(db: AsyncSession, now: datetime | None = None, batch: int = 500) -> int:
    now = now or datetime.now(timezone.utc)
    orphan_cutoff = now - timedelta(days=ORPHAN_DAYS)
    rows = list((await db.execute(select(GiftMedia).where(or_(
        (GiftMedia.status.in_(("pending", "uploaded"))) & (GiftMedia.created_at < orphan_cutoff),
        (GiftMedia.status == "linked") & (GiftMedia.expires_at < now),
    )).limit(batch))).scalars())
    deleted = await delete_media(db, rows)
    await db.commit()
    return deleted
