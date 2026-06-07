"""Dashboard + approve/reject route tests (offline; PM positions mocked)."""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from mochi_carry_signal import pm_client as pm_mod
from mochi_carry_signal.config import get_settings
from mochi_carry_signal.db import session_scope
from mochi_carry_signal.models import Signal
from mochi_carry_signal.web import app

from .conftest import make_points


def _client():
    # Don't trigger the lifespan poller during the test; we exercise routes only.
    return TestClient(app, raise_server_exceptions=True)


def _seed_pending(asset="BTC", kind="OPEN", key="k-1") -> int:
    with session_scope() as db:
        sig = Signal(created_at=datetime(2026, 6, 7, tzinfo=timezone.utc),
                     asset=asset, kind=kind, trailing_avg_pph=1e-4,
                     trailing_avg_apr=30.0, funding_now_apr=25.0,
                     spot_available=True, status="pending",
                     idempotency_key=key)
        db.add(sig)
        db.flush()
        return sig.id


def test_dashboard_200_with_funding_signals_positions(monkeypatch, fake_hl):
    fake_hl.funding["BTC"] = make_points(
        [1e-4] * 80, base=datetime.now(timezone.utc))
    fake_hl.spot["BTC"] = True
    _seed_pending()

    # Mock PM positions.
    client = pm_mod.PMClient(get_settings())
    monkeypatch.setattr(client, "positions", lambda: [
        {"arb_id": 9, "asset": "BTC", "status": "open", "neutral": True,
         "pnl": {"funding_total": 1.5, "net": 1.2}},
    ])
    monkeypatch.setattr(pm_mod, "_client", client)

    with _client() as c:
        r = c.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Carry Signal" in body
    assert "BTC" in body
    assert "pending" in body          # the seeded signal status pill
    assert "arb_id" in body
    assert "9" in body                # the open position
    assert r.headers["cache-control"].startswith("no-store")


def test_healthz():
    with _client() as c:
        assert c.get("/healthz").text == "ok"


def test_approve_route_without_secret_401(monkeypatch):
    sid = _seed_pending()
    # Offline stub PM (default client) — but auth should fail first.
    with _client() as c:
        r = c.post(f"/signals/{sid}/approve", data={"secret": "WRONG"},
                   follow_redirects=False)
    assert r.status_code == 401
    with session_scope() as db:
        assert db.get(Signal, sid).status == "pending"


def test_approve_route_with_secret_fires_and_redirects(monkeypatch, spy_notifier):
    sid = _seed_pending()
    with _client() as c:
        r = c.post(f"/signals/{sid}/approve",
                   data={"secret": get_settings().app_secret},
                   follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        assert db.get(Signal, sid).status == "fired"


def test_reject_route_marks_rejected(monkeypatch, spy_notifier):
    sid = _seed_pending()
    with _client() as c:
        r = c.post(f"/signals/{sid}/reject",
                   data={"secret": get_settings().app_secret},
                   follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        assert db.get(Signal, sid).status == "rejected"


def test_approve_unknown_signal_409(monkeypatch):
    with _client() as c:
        r = c.post("/signals/99999/approve",
                   data={"secret": get_settings().app_secret},
                   follow_redirects=False)
    assert r.status_code == 409
