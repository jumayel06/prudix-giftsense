"""Unit tests for the structlog -> Sentry warning bridge (app.main._forward_to_sentry).

structlog uses PrintLoggerFactory, so Sentry's LoggingIntegration never sees
these events. `_forward_to_sentry` forwards WARNING/ERROR/CRITICAL log calls to
Sentry as messages. These tests lock in: level filtering, DSN gating, extra
context attachment, and that the processor returns the event_dict unchanged.
"""

import contextlib
from unittest.mock import MagicMock

from app.main import _forward_to_sentry


def _event(level, **extra):
    return {"event": "some_event", "level": level, "timestamp": "2026-01-01T00:00:00Z", **extra}


def _patch_sentry(monkeypatch, dsn="https://k@o0.ingest.sentry.io/1"):
    """Enable the bridge + capture calls to sentry_sdk. Returns the capture mock + scope mock."""
    monkeypatch.setattr("app.main.settings.sentry_dsn", dsn)
    scope = MagicMock()

    @contextlib.contextmanager
    def _fake_new_scope():
        yield scope

    cap = MagicMock()
    monkeypatch.setattr("app.main.sentry_sdk.new_scope", _fake_new_scope)
    monkeypatch.setattr("app.main.sentry_sdk.capture_message", cap)
    return cap, scope


def test_warning_forwarded_with_context(monkeypatch):
    cap, scope = _patch_sentry(monkeypatch)

    out = _forward_to_sentry(None, "warning", _event("warning", shop_id="s1", foo="bar"))

    cap.assert_called_once()
    args, kwargs = cap.call_args
    assert args[0] == "some_event"          # grouped by log event name
    assert kwargs["level"] == "warning"
    # structured fields attached as extras; meta keys excluded
    scope.set_extra.assert_any_call("shop_id", "s1")
    scope.set_extra.assert_any_call("foo", "bar")
    scope.set_tag.assert_called_once_with("log_event", "some_event")
    assert out["event"] == "some_event"     # processor passes the dict through


def test_error_and_critical_map_to_error_level(monkeypatch):
    cap, _ = _patch_sentry(monkeypatch)
    _forward_to_sentry(None, "error", _event("error"))
    _forward_to_sentry(None, "critical", _event("critical"))
    assert [c.kwargs["level"] for c in cap.call_args_list] == ["error", "error"]


def test_info_and_debug_not_forwarded(monkeypatch):
    cap, _ = _patch_sentry(monkeypatch)
    _forward_to_sentry(None, "info", _event("info"))
    _forward_to_sentry(None, "debug", _event("debug"))
    cap.assert_not_called()


def test_noop_when_dsn_unset(monkeypatch):
    cap, _ = _patch_sentry(monkeypatch, dsn="")
    out = _forward_to_sentry(None, "error", _event("error"))
    cap.assert_not_called()
    assert out["event"] == "some_event"


def test_capture_failure_is_swallowed(monkeypatch):
    """A Sentry hiccup must never break the logging pipeline."""
    monkeypatch.setattr("app.main.settings.sentry_dsn", "https://k@o0.ingest.sentry.io/1")

    def _boom(*a, **k):
        raise RuntimeError("sentry down")

    monkeypatch.setattr("app.main.sentry_sdk.new_scope", _boom)
    # Should not raise; returns the event unchanged.
    out = _forward_to_sentry(None, "warning", _event("warning"))
    assert out["event"] == "some_event"
