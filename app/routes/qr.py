"""GET /qr/{view_token}.png: the QR code of a voice/video message's recipient
page, as an image merchants can print on their own packing slips (Shopify's
packing-slip template reads the `giftsense.messages` order metafield, which
lists these URLs; the help snippet is on the dashboard's Voice & video page).

No login: the view token is the same unguessable 128-bit secret the QR itself
encodes, and only linked, unexpired recordings get a code.
"""
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import media
from core.db.models import GiftMedia, Shop
from core.db.session import get_db

router = APIRouter()


def qr_image_url(host: str, view_token: str) -> str:
    return f"https://{host}/qr/{view_token}.png"


@router.get("/qr/{view_token}.png")
async def qr_png(view_token: str, db: AsyncSession = Depends(get_db)):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", view_token):
        raise HTTPException(404)
    row = (await db.execute(select(GiftMedia, Shop.shop_domain).join(Shop, Shop.id == GiftMedia.shop_id).where(
        GiftMedia.view_token == view_token, GiftMedia.status == "linked"))).first()
    if row is None:
        raise HTTPException(404)
    import segno
    buf = io.BytesIO()
    segno.make(media.view_url(row[1], view_token), error="m").save(buf, kind="png", scale=8, border=2)
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=86400", "X-Robots-Tag": "noindex"})
