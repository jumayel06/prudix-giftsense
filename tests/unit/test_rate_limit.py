"""Storefront abuse limits (fixed windows in Redis; fail open)."""
import pytest

from app.services import rate_limit as rl


@pytest.mark.asyncio
async def test_allows_up_to_the_limit_then_blocks(rate_store):
    results = [await rl.hit("search:sid:s1", limit=3, window_secs=3600) for _ in range(5)]
    assert results == [True, True, True, False, False]


@pytest.mark.asyncio
async def test_keys_are_independent(rate_store):
    for _ in range(3):
        await rl.hit("a", limit=3, window_secs=3600)
    assert await rl.hit("b", limit=3, window_secs=3600)


@pytest.mark.asyncio
async def test_windows_roll_over(rate_store, monkeypatch):
    monkeypatch.setattr(rl.time, "time", lambda: 1000.0)
    assert await rl.hit("k", limit=1, window_secs=60)
    assert not await rl.hit("k", limit=1, window_secs=60)
    monkeypatch.setattr(rl.time, "time", lambda: 1061.0)
    assert await rl.hit("k", limit=1, window_secs=60)


@pytest.mark.asyncio
async def test_fails_open_when_redis_is_down(rate_store):
    # Spend is still bounded by metering; a Redis outage must not break the widget.
    rate_store.down = True
    assert await rl.hit("k", limit=1, window_secs=60)
    assert await rl.hit("k", limit=1, window_secs=60)


@pytest.mark.parametrize("xff,expected", [
    ("203.0.113.9, 162.158.1.1, 10.0.0.1", "203.0.113.9"),   # shopper first, proxies appended
    ("203.0.113.9", "203.0.113.9"),
    ("", None), (None, None),
])
def test_shopper_ip_is_the_first_forwarded_address(xff, expected):
    assert rl.shopper_ip(xff) == expected


def test_ip_hash_is_stable_salted_and_not_the_ip():
    h = rl.ip_hash("203.0.113.9")
    assert h == rl.ip_hash("203.0.113.9") and "203" not in h and len(h) == 16
    assert rl.ip_hash("203.0.113.10") != h
