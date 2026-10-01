"""Parsing orders/create payloads into gift orders (app/services/gift_orders.py)."""
import json
import uuid

import pytest

from app.services import gift_orders as go

SID = str(uuid.uuid4())


def order(lines=None, attrs=None, **kw):
    return {
        "id": 5551, "admin_graphql_api_id": "gid://shopify/Order/5551", "name": "#1001",
        "total_price": "120.00", "currency": "USD",
        "note_attributes": [{"name": k, "value": v} for k, v in (attrs or {}).items()],
        "line_items": lines or [],
        "customer": {"id": 42, "email": "never@read.me"},   # present in real payloads; never stored
        **kw,
    }


def line(price="30.00", qty=1, props=None, product_id=1):
    return {"id": 9, "product_id": product_id, "variant_id": 7, "title": "Candle", "price": price, "quantity": qty,
            "properties": [{"name": k, "value": v} for k, v in (props or {}).items()]}


def test_plain_order_is_not_a_gift():
    assert go.parse_gift_order(order([line()])) is None


def test_gift_finder_line_makes_a_gift_order():
    p = go.parse_gift_order(order([line(props={"_giftsense_sid": SID, "_giftsense_gift": "1"}), line("50.00")],
                                  attrs={"_giftsense_sid": SID}))
    assert p.order_id == "5551" and p.order_name == "#1001" and str(p.sid) == SID
    assert p.gift_lines == 1 and p.gift_revenue == 30.0 and p.order_total == 120.0 and p.currency == "USD"


def test_gift_note_attribute_alone_makes_a_gift_order():
    p = go.parse_gift_order(order([line()], attrs={"Gift note": "Happy birthday!"}))
    assert p is not None and p.note == "Happy birthday!" and p.sid is None


def test_groups_are_parsed_defensively():
    groups = [{"id": "g1", "label": "Mom", "note": "Love you", "wrap": "gold"},
              {"id": "g2", "label": "x" * 200}, "junk"]
    p = go.parse_gift_order(order([line(props={"Gift for": "Mom", "_giftsense_gift": "g1"})],
                                  attrs={"_giftsense_gifts": json.dumps(groups), "_giftsense_mode": "self"}))
    assert p.delivery_mode == "self"
    assert [g["label"] for g in p.groups] == ["Mom", "x" * 40]
    assert p.groups[0]["note"] == "Love you"


def test_bad_json_and_bad_sid_are_ignored():
    p = go.parse_gift_order(order([line(props={"_giftsense_gift": "1"})],
                                  attrs={"_giftsense_gifts": "{not json", "_giftsense_sid": "nope"}))
    assert p is not None and p.groups == [] and p.sid is None


def test_customer_data_is_never_read():
    p = go.parse_gift_order(order([line(props={"_giftsense_gift": "1"})]))
    assert "never@read.me" not in json.dumps(p.__dict__, default=str) and "42" not in str(p.__dict__.get("sid"))


@pytest.mark.parametrize("final,draft,expected", [
    ("Happy birthday, Mom!", "Happy birthday, Mom!", "ai_accepted"),
    ("  happy birthday,  mom! ", "Happy birthday, Mom!", "ai_accepted"),   # whitespace/case
    ("Happy birthday, dear Mom!", "Happy birthday, Mom!", "ai_edited"),
    ("Enjoy the snow, love Sam", "Happy birthday, Mom!", "manual"),
    ("Anything", None, "manual"),
    (None, "Happy birthday", None),
])
def test_note_source(final, draft, expected):
    assert go.note_source(final, draft) == expected


def test_direct_mode_wrap_is_recorded_and_wrap_lines_are_not_gift_lines():
    p = go.parse_gift_order(order([line(props={"_giftsense_gift": "1"}),
                                   line(price="5.00", props={"_giftsense_wrap_for": "direct", "Wrap for": "Gift"})],
                                  attrs={"Gift wrap": "Gold", "_giftsense_mode": "direct"}))
    assert p.wrap == "Gold" and p.gift_lines == 1 and p.gift_revenue == 30.0
    assert go.metafield_value(p)["wrap"] == "Gold"


def test_delivery_mode_is_implied_without_the_old_attribute():
    """Since 2026-10-01 the widget no longer writes _giftsense_mode (fewer
    fields cluttering the order in Shopify admin)."""
    direct = go.parse_gift_order(order([line(props={"_giftsense_gift": "order"})],
                                       attrs={"_giftsense_sid": SID, "Gift note": "Happy anniversary"}))
    assert direct.delivery_mode == "direct"
    groups = '[{"id":"g1","label":"Mom"}]'
    self_mode = go.parse_gift_order(order([line(props={"_giftsense_gift": "g1", "Gift for": "Mom"})],
                                          attrs={"_giftsense_sid": SID, "_giftsense_gifts": groups}))
    assert self_mode.delivery_mode == "self"
    legacy = go.parse_gift_order(order([line(props={"_giftsense_gift": "order"})],
                                       attrs={"_giftsense_mode": "self"}))
    assert legacy.delivery_mode == "self"                     # older carts still parse the same
