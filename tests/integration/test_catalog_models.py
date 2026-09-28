"""Catalog storage: catalog_products (+ vector column) and catalog_syncs."""
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.purge import purge_shop_data
from core.db.models import CatalogProductRow, CatalogSync
from core.db.types import Embedding, _PgVector
from tests.conftest import make_shop


def _row(shop, pid="1", **kw):
    return CatalogProductRow(id=uuid.uuid4(), shop_id=shop.id, product_id=pid, title=f"Product {pid}",
                             price_min=20, price_max=30, content_hash="h1", **kw)


@pytest.mark.asyncio
async def test_product_row_round_trips_embedding_and_profile(db_session):
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add(_row(shop, embedding=[0.1, 0.2, 0.3], gift_profile={"vibes": ["cozy"]},
                        merchant_overrides={"giftable": 1.0}, tags=["cozy", "gift"]))
    await db_session.commit()

    r = (await db_session.execute(select(CatalogProductRow))).scalar_one()
    assert r.embedding == [0.1, 0.2, 0.3]
    assert r.gift_profile == {"vibes": ["cozy"]}
    assert r.tags == ["cozy", "gift"]
    assert r.excluded is False and r.available is True


@pytest.mark.asyncio
async def test_one_row_per_shop_and_product(db_session):
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add_all([_row(shop, "7"), _row(shop, "7")])
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_purge_removes_catalog_data(db_session):
    shop = make_shop(plan_status="uninstalled")
    db_session.add(shop)
    await db_session.flush()
    db_session.add_all([_row(shop), CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="initial", status="done")])
    await db_session.commit()

    await purge_shop_data(shop.id, db_session)
    assert (await db_session.execute(select(CatalogProductRow))).scalars().all() == []
    assert (await db_session.execute(select(CatalogSync))).scalars().all() == []


def test_postgres_vector_column_and_text_format():
    t = Embedding(512)
    impl = t.load_dialect_impl(postgresql.dialect())
    assert isinstance(impl, _PgVector) and impl.get_col_spec() == "vector(512)"
    bind = _PgVector(3).bind_processor(postgresql.dialect())
    assert bind([0.5, 1, -2.25]) == "[0.5,1.0,-2.25]"
    result = _PgVector(3).result_processor(postgresql.dialect(), None)
    assert result("[0.5,1,-2.25]") == [0.5, 1.0, -2.25]
    assert result(None) is None
