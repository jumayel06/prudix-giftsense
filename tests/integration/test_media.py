"""Voice/video messages: upload URL → confirm → linked on orders/create →
purged (app/services/media.py). R2 is a fake; nothing leaves the process."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.services import media
from core.config import settings
from core.db.models import GiftMedia
from tests.conftest import make_shop
from tests.integration.test_gift_order_webhook import post
from tests.integration.test_storefront import call
from tests.unit.test_gift_orders_parse import line, order

SID = str(uuid.uuid4())


class FakeR2:
    """Just enough of boto3's S3 client: objects are {key: size}."""

    def __init__(self):
        self.objects: dict[str, int] = {}
        self.deleted: list[str] = []

    def generate_presigned_url(self, op, Params, ExpiresIn):
        if op == "get_object":
            assert ExpiresIn == media.VIEW_URL_TTL_SECS
            return f"https://r2.example/get/{Params['Key']}?sig=1"
        assert op == "put_object" and ExpiresIn == media.UPLOAD_URL_TTL_SECS
        return f"https://r2.example/{Params['Key']}?sig=1&ct={Params['ContentType']}"

    def head_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": self.objects[Key]}

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.deleted.append(o["Key"])
            self.objects.pop(o["Key"], None)

    def get_paginator(self, name):
        objects = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}
        return P()


@pytest.fixture
def r2(monkeypatch):
    fake = FakeR2()
    for k, v in {"r2_account_id": "acct", "r2_access_key_id": "id", "r2_secret_access_key": "secret",
                 "r2_bucket": "giftsense-test"}.items():
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(media, "_client", lambda: fake)
    return fake


async def shop_on(db, plan="growth", **kw):
    shop = make_shop(plan_tier=plan, **kw)
    db.add(shop)
    await db.commit()
    return shop


def upload(db, kind="voice", mime="audio/webm;codecs=opus", size=200_000, duration_s=20):
    return call(db, "POST", "/api/storefront/media/upload-url",
                json={"sid": SID, "kind": kind, "mime": mime, "size": size, "duration_s": duration_s})


def confirm(db, token):
    return call(db, "POST", "/api/storefront/media/confirm", json={"sid": SID, "token": token})


# ── Config and gating ───────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("plan,kinds", [("starter", []), ("growth", ["voice"]), ("pro", ["voice", "video"])])
async def test_config_offers_media_by_plan(db_session, r2, plan, kinds):
    await shop_on(db_session, plan)
    assert call(db_session, "GET", "/api/storefront/config").json()["media"]["kinds"] == kinds


@pytest.mark.asyncio
async def test_nothing_offered_until_r2_is_configured(db_session):
    await shop_on(db_session, "pro")
    assert call(db_session, "GET", "/api/storefront/config").json()["media"]["kinds"] == []
    assert upload(db_session).status_code == 422


@pytest.mark.asyncio
async def test_video_needs_pro(db_session, r2):
    await shop_on(db_session, "growth")
    resp = upload(db_session, kind="video", mime="video/webm")
    assert resp.status_code == 422 and "plan" not in resp.json()["detail"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"mime": "image/png"},
    {"size": media.MAX_BYTES + 1},
    {"duration_s": 200},                    # voice max 120 s
])
async def test_bad_recordings_are_refused(db_session, r2, body):
    await shop_on(db_session)
    assert upload(db_session, **body).status_code == 422
    assert (await db_session.execute(select(GiftMedia))).first() is None


@pytest.mark.asyncio
async def test_monthly_quota(db_session, r2, monkeypatch):
    shop = await shop_on(db_session, plan_status="trial_active")
    monkeypatch.setattr(media, "monthly_limit", lambda s: 1)
    db_session.add(GiftMedia(shop_id=shop.id, token="t" * 22, view_token="v" * 22, kind="voice", storage_key="k",
                             mime="audio/webm", status="uploaded"))
    await db_session.commit()
    assert upload(db_session).status_code == 422


def test_trial_limit_is_the_smaller_one():
    shop = make_shop(plan_tier="growth", plan_status="trial_active")
    assert media.monthly_limit(shop) == 5
    shop.plan_status = "active"
    assert media.monthly_limit(shop) == 200


# ── Upload → confirm ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upload_and_confirm(db_session, r2):
    shop = await shop_on(db_session)
    resp = upload(db_session)
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    data = resp.json()
    row = (await db_session.execute(select(GiftMedia))).scalar_one()
    assert row.status == "pending" and row.storage_key == f"{shop.id}/{data['token']}.webm"
    assert data["upload_url"].startswith("https://r2.example/") and data["headers"] == {"Content-Type": "audio/webm"}
    assert row.view_token != row.token and len(row.view_token) >= 22          # 128-bit, separate from the cart token

    assert confirm(db_session, data["token"]).status_code == 422             # nothing uploaded yet
    r2.objects[row.storage_key] = 180_000
    ok = confirm(db_session, data["token"])
    assert ok.status_code == 200 and ok.json() == {"token": data["token"], "kind": "voice"}
    await db_session.refresh(row)
    assert row.status == "uploaded" and row.bytes == 180_000


@pytest.mark.asyncio
async def test_oversized_upload_is_deleted_on_confirm(db_session, r2):
    await shop_on(db_session)
    token = upload(db_session).json()["token"]
    row = (await db_session.execute(select(GiftMedia))).scalar_one()
    r2.objects[row.storage_key] = media.MAX_BYTES + 10
    assert confirm(db_session, token).status_code == 422
    assert row.storage_key in r2.deleted
    assert (await db_session.execute(select(GiftMedia))).first() is None


@pytest.mark.asyncio
async def test_confirm_needs_the_same_session(db_session, r2):
    await shop_on(db_session)
    token = upload(db_session).json()["token"]
    resp = call(db_session, "POST", "/api/storefront/media/confirm", json={"sid": str(uuid.uuid4()), "token": token})
    assert resp.status_code == 422


# ── Order link ──────────────────────────────────────────────────────────────

async def uploaded(db, shop, token, **kw):
    row = GiftMedia(shop_id=shop.id, token=token, view_token=f"view-{token}", kind="voice",
                    storage_key=f"{shop.id}/{token}.webm", mime="audio/webm", status="uploaded", **kw)
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
async def test_orders_link_their_messages(db_session, job_pool):
    shop = await shop_on(db_session)
    direct = await uploaded(db_session, shop, "direct-token-0001")
    mom = await uploaded(db_session, shop, "group-token-00001")
    stranger = await uploaded(db_session, shop, "unused-token-0001")
    other_shop = make_shop(domain="other.myshopify.com")
    db_session.add(other_shop)
    await db_session.commit()
    foreign = await uploaded(db_session, other_shop, "foreign-token-001")

    groups = '[{"id": "g1", "label": "Mom", "message": "group-token-00001"}, {"id": "g2", "message": "foreign-token-001"}]'
    post(db_session, "orders/create", order([line(props={"_giftsense_gift": "g1"})],
                                            attrs={"_giftsense_gifts": groups, "_giftsense_message": "direct-token-0001"}))
    for row in (direct, mom, stranger, foreign):
        await db_session.refresh(row)
    assert direct.status == mom.status == "linked" and mom.order_id == "5551"
    assert mom.expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) + timedelta(days=media.MEDIA_RETENTION_DAYS - 1)
    assert stranger.status == "uploaded" and foreign.status == "uploaded"      # other shop's token never links


def test_retention_counts_from_the_arrive_by_date():
    now = datetime(2026, 10, 4, tzinfo=timezone.utc)
    assert media.retention_end("2026-11-01", now) == datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(days=90)
    assert media.retention_end(None, now) == now + timedelta(days=90)
    assert media.retention_end("garbage", now) == now + timedelta(days=90)


# ── Purges ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_expired(db_session, r2):
    shop = await shop_on(db_session)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=media.ORPHAN_DAYS + 1)
    orphan = await uploaded(db_session, shop, "orphan-token-0001", created_at=old)
    fresh = await uploaded(db_session, shop, "fresh-token-00001")
    expired = await uploaded(db_session, shop, "expired-token-001", created_at=old)
    expired.status, expired.expires_at = "linked", now - timedelta(days=1)
    kept = await uploaded(db_session, shop, "kept-token-000001", created_at=old)
    kept.status, kept.expires_at = "linked", now + timedelta(days=10)
    await db_session.commit()
    for r in (orphan, fresh, expired, kept):
        r2.objects[r.storage_key] = 1

    assert await media.purge_expired(db_session) == 2
    left = set((await db_session.execute(select(GiftMedia.token))).scalars())
    assert left == {"fresh-token-00001", "kept-token-000001"}
    assert set(r2.deleted) == {orphan.storage_key, expired.storage_key}


@pytest.mark.asyncio
async def test_failed_r2_delete_keeps_the_rows(db_session, r2, monkeypatch):
    shop = await shop_on(db_session)
    await uploaded(db_session, shop, "orphan-token-0001", created_at=datetime.now(timezone.utc) - timedelta(days=30))

    def boom(**kw):
        raise RuntimeError("r2 down")
    monkeypatch.setattr(r2, "delete_objects", boom)
    with pytest.raises(RuntimeError):
        await media.purge_expired(db_session)
    await db_session.rollback()
    assert (await db_session.execute(select(GiftMedia))).first() is not None


@pytest.mark.asyncio
async def test_shop_purge_deletes_every_file_under_the_shop(db_session, r2):
    from app.purge import purge_shop_data
    shop = await shop_on(db_session)
    await uploaded(db_session, shop, "a-token-000000001")
    r2.objects.update({f"{shop.id}/a-token-000000001.webm": 1, f"{shop.id}/never-confirmed.webm": 1,
                       "someone-else/x.webm": 1})
    await purge_shop_data(shop.id, db_session)
    await db_session.commit()
    assert set(r2.objects) == {"someone-else/x.webm"}
    assert (await db_session.execute(select(GiftMedia))).first() is None


@pytest.mark.asyncio
async def test_customers_redact_deletes_the_orders_recordings(db_session, r2):
    from app.services.gdpr import redact_customer
    shop = await shop_on(db_session)
    linked = await uploaded(db_session, shop, "linked-token-0001", order_id="5551")
    other = await uploaded(db_session, shop, "other-token-00001", order_id="9999")
    r2.objects.update({linked.storage_key: 1, other.storage_key: 1})
    counts = await redact_customer(shop.id, {"customer": {"id": 42}, "orders_to_redact": [5551]}, db_session)
    assert counts["gift_media"] == 1 and r2.deleted == [linked.storage_key]
    assert set((await db_session.execute(select(GiftMedia.token))).scalars()) == {"other-token-00001"}


# ── Recipient page and the order metafield ──────────────────────────────────

@pytest.mark.asyncio
async def test_recipient_page_plays_the_message(db_session, r2):
    shop = await shop_on(db_session)
    row = await uploaded(db_session, shop, "page-token-00001", order_id="5551")
    row.status, row.expires_at = "linked", datetime.now(timezone.utc) + timedelta(days=5)
    await db_session.commit()
    resp = call(db_session, "GET", f"/api/storefront/m/{row.view_token}")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/liquid")
    assert resp.headers["x-robots-tag"] == "noindex, nofollow" and "noindex" in resp.text
    assert "{% layout none %}" in resp.text and "{{ shop.name | escape }}" in resp.text
    assert "<audio" in resp.text and f"https://r2.example/get/{row.storage_key}?sig=1" in resp.text
    assert "GiftSense" not in resp.text
    await db_session.refresh(row)
    assert row.view_count == 1


@pytest.mark.asyncio
async def test_recipient_page_hides_what_it_shouldnt_show(db_session, r2):
    shop = await shop_on(db_session)
    unlinked = await uploaded(db_session, shop, "unlinked-token-01")
    expired = await uploaded(db_session, shop, "expired-token-001")
    expired.status, expired.expires_at = "linked", datetime.now(timezone.utc) - timedelta(days=1)
    other = make_shop(domain="other.myshopify.com")
    db_session.add(other)
    await db_session.commit()
    foreign = await uploaded(db_session, other, "foreign-token-001")
    foreign.status = "linked"
    await db_session.commit()
    for token in (unlinked.view_token, expired.view_token, foreign.view_token, "not-a-real-token-123", "x" * 60):
        resp = call(db_session, "GET", f"/api/storefront/m/{token}")
        assert resp.status_code == 404 and "isn't available" in resp.text, token
    assert call(db_session, "GET", "/api/storefront/m/../../etc").status_code == 404


@pytest.mark.asyncio
async def test_order_metafield_carries_the_message_link(db_session, job_pool):
    shop = await shop_on(db_session)
    await uploaded(db_session, shop, "direct-token-0001")
    post(db_session, "orders/create", order([line(props={"_giftsense_gift": "order"})],
                                            attrs={"_giftsense_message": "direct-token-0001"}))
    value = job_pool.jobs[0][3]
    assert value["message"] == "direct-token-0001" and value["message_kind"] == "voice"
    assert value["message_url"] == f"https://{shop.shop_domain}/apps/giftsense/m/view-direct-token-0001"


# ── Dashboard: Voice & video page ───────────────────────────────────────────

def dashboard_get(db, path):
    from fastapi.testclient import TestClient
    from app.main import app
    from core.db.session import get_db
    from tests.conftest import TEST_SHOP_DOMAIN

    async def override_db():
        yield db
    app.dependency_overrides[get_db] = override_db
    try:
        return TestClient(app).get(f"{path}?shop={TEST_SHOP_DOMAIN}")
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_messages_page_shows_usage_and_linked_recordings(db_session, r2):
    from core.db.models import GiftOrder
    shop = await shop_on(db_session, "pro")
    await uploaded(db_session, shop, "pending-token-001")
    row = await uploaded(db_session, shop, "linked-token-0001", order_id="5551", duration_s=42, view_count=3)
    row.status, row.expires_at = "linked", datetime.now(timezone.utc) + timedelta(days=90)
    db_session.add(GiftOrder(shop_id=shop.id, order_id="5551", order_name="#1001"))
    await db_session.commit()
    data = dashboard_get(db_session, "/api/messages").json()
    assert data["voice"] and data["video"] and data["available"]
    assert data["used"] == 2 and data["limit"] == 500
    assert [(m["order_name"], m["kind"], m["views"], m["duration_s"]) for m in data["messages"]] == [("#1001", "voice", 3, 42)]


@pytest.mark.asyncio
async def test_messages_page_on_starter(db_session):
    await shop_on(db_session, "starter")
    data = dashboard_get(db_session, "/api/messages").json()
    assert data["voice"] is False and data["available"] is False and data["messages"] == []


# ── Packing slips ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_order_lists_messages_for_packing_slips(db_session, job_pool):
    shop = await shop_on(db_session)
    await uploaded(db_session, shop, "mom-token-000001")
    groups = '[{"id": "g1", "label": "Mom", "message": "mom-token-000001"}, {"id": "g2", "label": "Dad"}]'
    post(db_session, "orders/create", order([line(props={"_giftsense_gift": "g1"})], attrs={"_giftsense_gifts": groups}))
    msgs = job_pool.jobs[0][3]["messages"]
    assert msgs == [{"for": "Mom", "kind": "voice",
                     "url": f"https://{shop.shop_domain}/apps/giftsense/m/view-mom-token-000001",
                     "qr": f"https://{settings.get_app_host()}/qr/view-mom-token-000001.png"}]


@pytest.mark.asyncio
async def test_annotate_writes_the_packing_slip_metafield():
    from unittest.mock import AsyncMock, MagicMock
    from app.services.gift_orders import annotate_order
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"data": {"tagsAdd": {"userErrors": []}, "metafieldsSet": {"userErrors": []}}}
    gql = AsyncMock(return_value=ok)
    await annotate_order("s", "t", "gid://shopify/Order/1", {"groups": [], "messages": [{"for": "Mom", "qr": "q"}]}, gql=gql)
    fields = gql.await_args_list[1].args[3]["metafields"]
    assert [(f["namespace"], f["key"]) for f in fields] == [("$app:giftsense", "gifts"), ("giftsense", "messages")]
    gql.reset_mock()
    await annotate_order("s", "t", "gid://shopify/Order/1", {"groups": []}, gql=gql)
    assert len(gql.await_args_list[1].args[3]["metafields"]) == 1          # no messages, no extra metafield


@pytest.mark.asyncio
async def test_qr_image(db_session):
    from fastapi.testclient import TestClient
    from app.main import app
    from core.db.session import get_db
    shop = await shop_on(db_session)
    row = await uploaded(db_session, shop, "qr-token-0000001")
    pending = await uploaded(db_session, shop, "qr-token-0000002")
    row.status = "linked"
    await db_session.commit()

    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        c = TestClient(app)
        ok = c.get(f"/qr/{row.view_token}.png")
        assert ok.status_code == 200 and ok.headers["content-type"] == "image/png" and ok.content[:4] == b"\x89PNG"
        assert c.get(f"/qr/{pending.view_token}.png").status_code == 404      # not on an order
        assert c.get("/qr/not-a-real-token-xyz.png").status_code == 404
    finally:
        app.dependency_overrides.clear()
