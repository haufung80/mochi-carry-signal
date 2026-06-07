"""Telegram notifier: builds the right message, best-effort (failure swallowed),
and never sends in offline mode.
"""
import httpx

from mochi_carry_signal.notifier import TelegramNotifier


def test_disabled_when_offline_even_with_creds():
    # TESTING=true in conftest => offline => disabled regardless of creds.
    n = TelegramNotifier(token="t", chat_id="c")
    assert n.enabled is False
    # send() is a no-op (no exception, no network).
    n.signal_generated("BTC", "OPEN", 30.0, 25.0)


def test_disabled_without_creds():
    n = TelegramNotifier(token="", chat_id="")
    assert n.enabled is False


def test_send_is_best_effort_when_enabled(monkeypatch):
    """Even when 'enabled', an httpx failure is swallowed (never raised)."""
    n = TelegramNotifier(token="t", chat_id="c")
    # Force-enable past the offline guard for this test.
    monkeypatch.setattr(n, "_offline", False)
    assert n.enabled is True

    class BoomClient:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **kw):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "Client", BoomClient)
    # Must not raise.
    n.signal_generated("ETH", "CLOSE", -2.0, -1.0)


def test_message_format_captured(monkeypatch):
    """The message text carries asset/kind/avg-APR and uses the bot endpoint."""
    sent = {}
    n = TelegramNotifier(token="tok", chat_id="42")
    monkeypatch.setattr(n, "_offline", False)

    class CaptureClient:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, **kw):
            sent["url"] = url
            sent["payload"] = json
            return httpx.Response(200)

    monkeypatch.setattr(httpx, "Client", CaptureClient)
    n.signal_generated("BTC", "OPEN", 31.4, 28.0)

    assert "bottok/sendMessage" in sent["url"]
    assert sent["payload"]["chat_id"] == "42"
    text = sent["payload"]["text"]
    assert "OPEN" in text and "BTC" in text and "31.4" in text


def test_opened_and_closed_messages(monkeypatch):
    msgs = []
    n = TelegramNotifier(token="tok", chat_id="42")
    monkeypatch.setattr(n, "_offline", False)
    monkeypatch.setattr(n, "send", lambda text, **kw: msgs.append(text))
    n.opened("BTC", 777, 30.0)
    n.closed("ETH", 555)
    n.fire_error("SOL", "OPEN", "kaboom")
    assert "777" in msgs[0] and "OPEN fired" in msgs[0]
    assert "555" in msgs[1] and "CLOSE fired" in msgs[1]
    assert "kaboom" in msgs[2]
