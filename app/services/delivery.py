"""Arrive-by dates (Growth and Pro; docs/TECHNICAL_PLAN.md §6.5).

The merchant sets simple shipping rules; the widget offers only dates the
store can make ("Arrive by (estimated)"), and each order gets a ship-by date:
the latest shipping day that still arrives on time. Until then its
merchant-managed fulfillment orders are on hold (app/services/holds.py).

Rules (shops.gift_settings["delivery"]):
  enabled           show the date picker in the gift panel
  processing_days   shipping days needed to prepare an order (0 = same day)
  transit_days      calendar days in transit (an estimate, shown as such)
  ship_weekdays     days the store ships, 0 = Monday … 6 = Sunday
  blackout_dates    extra no-ship days (holidays), ISO dates
  max_days_ahead    how far ahead shoppers can pick
  cutoff_hour       orders after this hour (store timezone) count from tomorrow

Every date here is a calendar date in the store's timezone.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

DEFAULTS = {"enabled": False, "processing_days": 1, "transit_days": 3, "ship_weekdays": [0, 1, 2, 3, 4],
            "blackout_dates": [], "max_days_ahead": 60, "cutoff_hour": 14}
MAX_BLACKOUTS = 60
SEARCH_LIMIT_DAYS = 400          # safety bound for day-by-day searches


class DeliveryUpdate(BaseModel):
    enabled: Optional[bool] = None
    processing_days: Optional[int] = Field(default=None, ge=0, le=30)
    transit_days: Optional[int] = Field(default=None, ge=0, le=30)
    ship_weekdays: Optional[list[int]] = Field(default=None, min_length=1, max_length=7)
    blackout_dates: Optional[list[date]] = Field(default=None, max_length=MAX_BLACKOUTS)
    max_days_ahead: Optional[int] = Field(default=None, ge=7, le=180)
    cutoff_hour: Optional[int] = Field(default=None, ge=0, le=23)

    @field_validator("ship_weekdays")
    @classmethod
    def _weekdays(cls, days):
        if days is None:
            return None
        if any(d not in range(7) for d in days):
            raise ValueError("ship_weekdays: 0 (Monday) to 6 (Sunday)")
        return sorted(set(days))

    @field_validator("blackout_dates")
    @classmethod
    def _blackouts(cls, days):
        return None if days is None else sorted(set(days))


def delivery_settings(shop) -> dict:
    stored = (getattr(shop, "gift_settings", None) or {}).get("delivery") or {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


def update_delivery_settings(shop, update: DeliveryUpdate) -> None:
    current = delivery_settings(shop)
    current.update(update.model_dump(exclude_none=True, mode="json"))
    shop.gift_settings = {**(shop.gift_settings or {}), "delivery": current}


def store_today(tz: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    try:
        return now.astimezone(ZoneInfo(tz or "UTC"))
    except (KeyError, ValueError):
        return now.astimezone(timezone.utc)


class Rules:
    def __init__(self, settings: dict):
        self.processing = int(settings["processing_days"])
        self.transit = int(settings["transit_days"])
        self.weekdays = set(settings["ship_weekdays"]) or {0, 1, 2, 3, 4}
        self.blackouts = {date.fromisoformat(d) if isinstance(d, str) else d for d in settings["blackout_dates"]}
        self.max_ahead = int(settings["max_days_ahead"])
        self.cutoff = int(settings["cutoff_hour"])

    def ships_on(self, d: date) -> bool:
        return d.weekday() in self.weekdays and d not in self.blackouts

    def earliest_ship(self, local_now: datetime) -> date:
        """First day an order placed now can leave: after the cutoff it counts
        from tomorrow, then `processing_days` shipping days of preparation."""
        d = local_now.date() + timedelta(days=1 if local_now.hour >= self.cutoff else 0)
        need = self.processing
        for _ in range(SEARCH_LIMIT_DAYS):
            if self.ships_on(d):
                if need == 0:
                    return d
                need -= 1
            d += timedelta(days=1)
        return d

    def ship_by(self, arrive_by: date, not_before: date) -> date | None:
        """Latest shipping day that still arrives by `arrive_by`, or None if it
        would have to leave before `not_before` (date not achievable)."""
        d = arrive_by - timedelta(days=self.transit)
        while d >= not_before:
            if self.ships_on(d):
                return d
            d -= timedelta(days=1)
        return None


def date_window(settings: dict, tz: str, now: datetime | None = None) -> dict:
    """For the widget: the earliest and latest pickable arrive-by dates. Every
    date in between can be met (if the ideal ship day is closed, the order
    simply ships on an earlier open day)."""
    rules = Rules(settings)
    local = store_today(tz, now)
    earliest = rules.earliest_ship(local) + timedelta(days=rules.transit)
    latest = max(earliest, local.date() + timedelta(days=rules.max_ahead))
    return {"earliest": earliest.isoformat(), "latest": latest.isoformat()}


def plan_shipment(settings: dict, tz: str, arrive_by: str | None, now: datetime | None = None) -> dict | None:
    """For an order: {"arrive_by", "ship_by", "late"}. Ship-by is the latest
    shipping day that still arrives on time, never before the earliest day the
    store can ship. `late` = the date can't be met anymore (ship as soon as
    possible, don't hold). None when the date is missing or invalid."""
    try:
        target = date.fromisoformat(str(arrive_by or "")[:10])
    except ValueError:
        return None
    rules = Rules(settings)
    first_ship = rules.earliest_ship(store_today(tz, now))
    ship = rules.ship_by(target, first_ship)
    if ship is None:
        return {"arrive_by": target.isoformat(), "ship_by": first_ship.isoformat(), "late": True}
    return {"arrive_by": target.isoformat(), "ship_by": ship.isoformat(), "late": False}
