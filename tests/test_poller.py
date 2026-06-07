"""Poller tests: a transition records ONE pending Signal + (mocked) Telegram;
a repeat poll in the same state records NOTHING (idempotent).
"""
from datetime import timedelta

from sqlalchemy import func, select

from mochi_carry_signal.db import session_scope
from mochi_carry_signal.models import Signal
from mochi_carry_signal.poller import idempotency_key, poll_once
from mochi_carry_signal.signal import apr_to_pph

from .conftest import make_points

# A native per-hour rate well above the 10%/yr entry threshold (entry ≈ 1.14e-5).
HOT = apr_to_pph(30.0)      # 30%/yr trailing avg -> OPEN
COLD = apr_to_pph(-5.0)     # negative -> CLOSE


def _signal_count(db=None):
    with session_scope() as s:
        return s.execute(select(func.count(Signal.id))).scalar()


def test_flat_to_open_records_one_pending_and_alerts(fake_hl, spy_notifier, now):
    fake_hl.funding["BTC"] = make_points([HOT] * 80, base=now)   # 80h of hot funding
    fake_hl.spot["BTC"] = True

    new = poll_once(now=now)

    assert len(new) == 1
    sig = new[0]
    assert sig.asset == "BTC" and sig.kind == "OPEN" and sig.status == "pending"
    assert sig.trailing_avg_apr > 10.0
    assert sig.spot_available is True
    # exactly one row persisted
    assert _signal_count() == 1
    # telegram alert fired once
    assert [c[0] for c in spy_notifier.calls] == ["signal_generated"]


def test_repeat_poll_same_state_is_idempotent(fake_hl, spy_notifier, now):
    fake_hl.funding["BTC"] = make_points([HOT] * 80, base=now)
    fake_hl.spot["BTC"] = True

    first = poll_once(now=now)
    assert len(first) == 1
    # Same clock hour, still hot -> already OPEN -> no new signal.
    second = poll_once(now=now + timedelta(minutes=5))
    assert second == []
    assert _signal_count() == 1
    assert len([c for c in spy_notifier.calls if c[0] == "signal_generated"]) == 1


def test_spot_unavailable_suppresses_open(fake_hl, spy_notifier, now):
    fake_hl.funding["ETH"] = make_points([HOT] * 80, base=now)
    fake_hl.spot["ETH"] = False
    # Only ETH configured-relevant; restrict assets via settings override.
    from mochi_carry_signal.config import get_settings
    s = get_settings()
    new = poll_once(now=now, settings=s)
    # No OPEN for ETH (no spot); BTC/SOL have no funding data -> None.
    assert all(x.asset != "ETH" for x in new)
    assert _signal_count() == 0


def test_open_then_close_records_close(fake_hl, spy_notifier, now):
    # First poll: hot -> OPEN.
    fake_hl.funding["BTC"] = make_points([HOT] * 80, base=now)
    fake_hl.spot["BTC"] = True
    poll_once(now=now)
    assert _signal_count() == 1

    # Mark the OPEN as fired with an arb_id (so CLOSE can reference it).
    with session_scope() as db:
        sig = db.execute(select(Signal)).scalars().first()
        sig.status = "fired"
        sig.arb_id = 4242

    # Later poll: funding goes negative -> trailing avg <= 0 -> CLOSE.
    later = now + timedelta(hours=80)
    fake_hl.funding["BTC"] = make_points([COLD] * 80, base=later)
    fake_hl.spot["BTC"] = True
    new = poll_once(now=later)

    assert len(new) == 1
    close_sig = new[0]
    assert close_sig.kind == "CLOSE" and close_sig.status == "pending"
    # CLOSE picked up the arb_id from the fired OPEN.
    assert close_sig.arb_id == 4242
    assert _signal_count() == 2


def test_idempotency_key_is_deterministic_per_hour(now):
    k1 = idempotency_key("BTC", "OPEN", now)
    k2 = idempotency_key("BTC", "OPEN", now + timedelta(minutes=30))  # same hour
    k3 = idempotency_key("BTC", "OPEN", now + timedelta(hours=1))     # next hour
    assert k1 == k2
    assert k1 != k3
    assert k1.startswith("sig-") and k1.endswith("-BTC-OPEN")


def test_asset_fetch_failure_is_isolated(fake_hl, spy_notifier, now):
    fake_hl.funding["BTC"] = make_points([HOT] * 80, base=now)
    fake_hl.spot["BTC"] = True
    fake_hl.fail_assets = {"ETH"}   # ETH fetch raises
    new = poll_once(now=now)
    # BTC still recorded despite ETH blowing up.
    assert [s.asset for s in new] == ["BTC"]
