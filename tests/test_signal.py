"""Signal-logic tests: trailing-72h pph avg, right-closed / no look-ahead,
the OPEN/CLOSE thresholds, and the FLAT/OPEN state machine.

These guard the LOCKED rule and the #1 backtester bug source (never crossing
signal units, never leaking the future).
"""
from datetime import datetime, timedelta, timezone

import pytest

from mochi_carry_signal.signal import (
    HOURS_PER_YEAR,
    Settlement,
    apr_to_pph,
    compute_signal,
    decide,
    pph_to_apr,
)

NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


def _settles(rates, *, end=NOW, step_h=1):
    """Settlements every step_h hours ending at `end`, last rate at `end`."""
    n = len(rates)
    return [Settlement(time=end - timedelta(hours=step_h * (n - 1 - i)),
                       funding_rate_pph=r) for i, r in enumerate(rates)]


# --- unit conversion ---------------------------------------------------------

def test_apr_pph_roundtrip():
    assert HOURS_PER_YEAR == 24 * 365
    # apr_to_pph(10) is the entry threshold; round-trips.
    pph = apr_to_pph(10.0)
    assert pph == pytest.approx(10.0 / 100.0 / 8760.0)
    assert pph_to_apr(pph) == pytest.approx(10.0)


def test_pph_to_apr_none():
    assert pph_to_apr(None) is None


# --- trailing average --------------------------------------------------------

def test_trailing_avg_is_mean_of_window():
    s = _settles([0.001, 0.002, 0.003])      # 3 hourly settlements ending NOW
    avg = compute_signal(s, NOW, lookback_h=72)
    assert avg == pytest.approx((0.001 + 0.002 + 0.003) / 3)


def test_window_excludes_settlements_older_than_lookback():
    # One settlement 100h ago, two within the last 3h.
    old = Settlement(time=NOW - timedelta(hours=100), funding_rate_pph=99.0)
    recent = _settles([0.001, 0.002], end=NOW)
    avg = compute_signal([old, *recent], NOW, lookback_h=72)
    # The 100h-old huge value must be excluded.
    assert avg == pytest.approx((0.001 + 0.002) / 2)


def test_right_closed_boundary_excludes_exact_lower_edge():
    # A settlement at EXACTLY now - L is excluded (window is (now-L, now]).
    L = 72
    edge = Settlement(time=NOW - timedelta(hours=L), funding_rate_pph=5.0)
    inside = Settlement(time=NOW - timedelta(hours=L - 1), funding_rate_pph=1.0)
    avg = compute_signal([edge, inside], NOW, lookback_h=L)
    assert avg == pytest.approx(1.0)          # edge excluded, only `inside`


def test_right_closed_includes_exact_now():
    # A settlement at exactly `now` IS included (right-closed].
    at_now = Settlement(time=NOW, funding_rate_pph=2.0)
    avg = compute_signal([at_now], NOW, lookback_h=72)
    assert avg == pytest.approx(2.0)


def test_no_look_ahead_future_settlement_never_changes_earlier_decision():
    # Decision at t0 must NOT see a settlement at t0 + 1h.
    base = _settles([0.001, 0.001], end=NOW)
    future = Settlement(time=NOW + timedelta(hours=1), funding_rate_pph=99.0)
    avg_without = compute_signal(base, NOW, lookback_h=72)
    avg_with_future = compute_signal([*base, future], NOW, lookback_h=72)
    assert avg_with_future == pytest.approx(avg_without)


def test_empty_window_returns_none():
    # No settlement in the window => undefined signal.
    far = Settlement(time=NOW - timedelta(hours=500), funding_rate_pph=1.0)
    assert compute_signal([far], NOW, lookback_h=72) is None
    assert compute_signal([], NOW, lookback_h=72) is None


def test_invalid_lookback():
    with pytest.raises(ValueError):
        compute_signal([], NOW, lookback_h=0)


# --- state machine -----------------------------------------------------------

ENTRY = apr_to_pph(10.0)
EXIT = apr_to_pph(0.0)   # == 0


def test_flat_opens_when_avg_ge_entry_and_spot():
    assert decide("FLAT", ENTRY, True, ENTRY, EXIT) == "OPEN"
    assert decide("FLAT", ENTRY * 2, True, ENTRY, EXIT) == "OPEN"


def test_flat_holds_when_below_entry():
    assert decide("FLAT", ENTRY * 0.5, True, ENTRY, EXIT) is None


def test_spot_unavailable_suppresses_open():
    # Even with avg >> entry, no spot => no OPEN (cash-and-carry needs spot leg).
    assert decide("FLAT", ENTRY * 5, False, ENTRY, EXIT) is None


def test_open_closes_when_avg_le_exit():
    assert decide("OPEN", 0.0, True, ENTRY, EXIT) == "CLOSE"
    assert decide("OPEN", -0.001, True, ENTRY, EXIT) == "CLOSE"


def test_open_holds_above_exit():
    assert decide("OPEN", apr_to_pph(3.0), True, ENTRY, EXIT) is None


def test_open_close_does_not_require_spot():
    # Closing is always allowed regardless of spot availability.
    assert decide("OPEN", -1.0, False, ENTRY, EXIT) == "CLOSE"


def test_none_avg_holds_both_states():
    assert decide("FLAT", None, True, ENTRY, EXIT) is None
    assert decide("OPEN", None, True, ENTRY, EXIT) is None
