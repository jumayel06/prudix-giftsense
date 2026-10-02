"""Arrive-by date rules (app/services/delivery.py)."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.services import delivery as dl
from tests.conftest import make_shop

# Wednesday 2026-10-07, 10:00 in New York (14:00 UTC).
WED_10AM = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)
NY = "America/New_York"


def rules(**kw):
    return {**dl.DEFAULTS, "enabled": True, **kw}


def test_defaults_are_off():
    assert dl.delivery_settings(make_shop())["enabled"] is False


def test_earliest_ship_respects_processing_and_weekends():
    # 1 processing day from Wed → ships Thu.
    assert dl.Rules(rules()).earliest_ship(dl.store_today(NY, WED_10AM)).isoformat() == "2026-10-08"
    # After the 14:00 cutoff, Wed counts as Thu → ships Fri.
    late = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)          # 16:00 NY
    assert dl.Rules(rules()).earliest_ship(dl.store_today(NY, late)).isoformat() == "2026-10-09"
    # Today counts as the first processing day: 3 days = Wed, Thu, Fri → ships Mon.
    assert dl.Rules(rules(processing_days=3)).earliest_ship(dl.store_today(NY, WED_10AM)).isoformat() == "2026-10-12"


def test_window_starts_after_processing_plus_transit():
    w = dl.date_window(rules(), NY, WED_10AM)
    assert w["earliest"] == "2026-10-11"                       # ships Thu + 3 days
    assert w["latest"] == "2026-12-06"                         # today + 60


def test_blackout_days_push_the_earliest_date():
    # Thu and Fri are blackouts: first ship day is Mon 10-12, so earliest arrival Thu 10-15.
    w = dl.date_window(rules(blackout_dates=["2026-10-08", "2026-10-09"]), NY, WED_10AM)
    assert w["earliest"] == "2026-10-15"


def test_closed_ideal_ship_day_ships_earlier_instead():
    # Arrive Fri 10-23: ideal ship Tue 10-20 is a blackout → ships Mon 10-19.
    s = dl.plan_shipment(rules(blackout_dates=["2026-10-20"]), NY, "2026-10-23", WED_10AM)
    assert s["ship_by"] == "2026-10-19" and s["late"] is False


def test_ship_by_is_the_latest_day_that_still_arrives():
    s = dl.plan_shipment(rules(), NY, "2026-10-20", WED_10AM)  # Tue 10-20 − 3 = Sat 10-17 → Fri 10-16
    assert s == {"arrive_by": "2026-10-20", "ship_by": "2026-10-16", "late": False}


def test_a_date_that_cannot_be_met_ships_as_soon_as_possible():
    s = dl.plan_shipment(rules(), NY, "2026-10-09", WED_10AM)
    assert s == {"arrive_by": "2026-10-09", "ship_by": "2026-10-08", "late": True}


@pytest.mark.parametrize("bad", [None, "", "next week", "2026-13-01"])
def test_invalid_dates_are_ignored(bad):
    assert dl.plan_shipment(rules(), NY, bad, WED_10AM) is None


def test_update_validates():
    shop = make_shop()
    dl.update_delivery_settings(shop, dl.DeliveryUpdate(enabled=True, ship_weekdays=[4, 0, 0, 2],
                                                        blackout_dates=["2026-12-25", "2026-12-24"]))
    s = dl.delivery_settings(shop)
    assert s["enabled"] and s["ship_weekdays"] == [0, 2, 4] and s["blackout_dates"] == ["2026-12-24", "2026-12-25"]
    for bad in ({"ship_weekdays": []}, {"ship_weekdays": [7]}, {"transit_days": -1}, {"max_days_ahead": 365}):
        with pytest.raises(ValidationError):
            dl.DeliveryUpdate(**bad)
