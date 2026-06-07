"""Approve-to-fire tests.

Asserts the EXACT PM wire request: an OPEN approve issues
``POST {PM}/funding-arb/open`` with the ``X-Arb-Secret`` header and a body
carrying ``size_mode:"min"`` + the signal's idempotency_key; arb_id/fired get
stored. A CLOSE approve issues ``/funding-arb/close``. Reject -> rejected.
Approve with a wrong APP_SECRET -> AuthError (401 at the route).

The PM HTTP call is captured via an httpx MockTransport installed on the
PMClient's ``_request`` seam, so we verify the real request the client would
put on the wire (NOT the offline stub).
"""
from datetime import datetime, timezone

import httpx
import pytest

from mochi_carry_signal import pm_client as pm_mod
from mochi_carry_signal.approval import AuthError, approve, reject
from mochi_carry_signal.config import get_settings
from mochi_carry_signal.db import session_scope
from mochi_carry_signal.models import Signal

KEY = "sig-2026-06-07T12:00:00Z-BTC-OPEN"


def _make_signal(kind="OPEN", status="pending", arb_id=None, asset="BTC",
                 key=KEY) -> int:
    with session_scope() as db:
        sig = Signal(
            created_at=datetime(2026, 6, 7, 12, tzinfo=timezone.utc),
            asset=asset, kind=kind, trailing_avg_pph=1e-4,
            trailing_avg_apr=30.0, funding_now_apr=25.0, spot_available=True,
            status=status, idempotency_key=key, arb_id=arb_id)
        db.add(sig)
        db.flush()
        return sig.id


@pytest.fixture
def wired_pm(monkeypatch):
    """Force the PMClient ONTO the wire (override offline) + capture requests.

    Returns a list of captured httpx.Request objects. The PM responds 200 with a
    canned ArbOpenResponse / ArbCloseResponse.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/open"):
            return httpx.Response(200, json={
                "status": "accepted", "arb_id": 777,
                "idempotency_key": KEY, "legs": []})
        if request.url.path.endswith("/close"):
            return httpx.Response(200, json={"status": "closing", "arb_id": 555})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = pm_mod.PMClient(get_settings())

    # Force the real network path even though TESTING=true, and route httpx
    # through the mock transport.
    def _request(method, path, *, json=None):
        url = client._url(path)
        with httpx.Client(transport=transport) as c:
            resp = c.request(method, url, json=json, headers=client._headers)
        if resp.status_code >= 400:
            raise pm_mod.PMError(f"{resp.status_code}: {resp.text}")
        return resp.json() if resp.content else None

    monkeypatch.setattr(client, "_request", _request)
    # Make open/close bypass the offline stub by calling _request directly.
    monkeypatch.setattr(client._s, "testing", False)
    monkeypatch.setattr(client._s, "dry_run", False)
    monkeypatch.setattr(pm_mod, "_client", client)
    return captured


def test_open_approve_issues_correct_open_request(wired_pm, spy_notifier):
    sid = _make_signal(kind="OPEN")
    updated = approve(sid, secret=get_settings().app_secret)

    assert len(wired_pm) == 1
    req = wired_pm[0]
    assert req.method == "POST"
    assert str(req.url) == "http://pm.test/funding-arb/open"
    assert req.headers["X-Arb-Secret"] == "test-arb-secret"
    import json as _json
    body = _json.loads(req.content)
    assert body["size_mode"] == "min"
    assert body["idempotency_key"] == KEY
    assert body["asset"] == "BTC"
    assert body["strategy_tag"] == "hl-cash-and-carry"
    assert "legs" not in body            # default combo => legs omitted
    assert "notional" not in body        # size_mode=min => no notional

    # arb_id stored + fired.
    assert updated.status == "fired"
    assert updated.arb_id == 777
    with session_scope() as db:
        row = db.get(Signal, sid)
        assert row.status == "fired" and row.arb_id == 777 and row.fired_at
    assert [c[0] for c in spy_notifier.calls] == ["opened"]


def test_close_approve_issues_close_request(wired_pm, spy_notifier):
    sid = _make_signal(kind="CLOSE", arb_id=555,
                       key="sig-2026-06-07T12:00:00Z-BTC-CLOSE")
    updated = approve(sid, secret=get_settings().app_secret)

    assert len(wired_pm) == 1
    req = wired_pm[0]
    assert str(req.url) == "http://pm.test/funding-arb/close"
    assert req.headers["X-Arb-Secret"] == "test-arb-secret"
    import json as _json
    assert _json.loads(req.content) == {"arb_id": 555}
    assert updated.status == "fired" and updated.arb_id == 555
    assert [c[0] for c in spy_notifier.calls] == ["closed"]


def test_approve_wrong_secret_raises_auth(spy_notifier):
    sid = _make_signal()
    with pytest.raises(AuthError):
        approve(sid, secret="WRONG")
    # Untouched.
    with session_scope() as db:
        assert db.get(Signal, sid).status == "pending"


def test_reject_marks_rejected_and_alerts(spy_notifier):
    sid = _make_signal()
    updated = reject(sid, secret=get_settings().app_secret)
    assert updated.status == "rejected"
    with session_scope() as db:
        assert db.get(Signal, sid).status == "rejected"
    assert [c[0] for c in spy_notifier.calls] == ["rejected"]


def test_pm_failure_marks_error_and_alerts(monkeypatch, spy_notifier):
    """A PM error on fire -> signal goes to 'error' + fire_error alert."""
    sid = _make_signal(kind="OPEN")

    client = pm_mod.PMClient(get_settings())

    def boom(**kw):
        raise pm_mod.PMError("pm exploded")

    monkeypatch.setattr(client, "open_arb", boom)
    monkeypatch.setattr(pm_mod, "_client", client)

    from mochi_carry_signal.approval import ApprovalError
    with pytest.raises(ApprovalError):
        approve(sid, secret=get_settings().app_secret)

    with session_scope() as db:
        row = db.get(Signal, sid)
        assert row.status == "error"
        assert "pm exploded" in row.error_message
    assert [c[0] for c in spy_notifier.calls] == ["fire_error"]


def test_offline_stub_open_does_not_touch_network(spy_notifier):
    """In TESTING mode the default client uses the offline stub (no network) and
    still fires the signal -> 'fired' with a deterministic arb_id."""
    sid = _make_signal(kind="OPEN")
    updated = approve(sid, secret=get_settings().app_secret)
    assert updated.status == "fired"
    assert isinstance(updated.arb_id, int)
