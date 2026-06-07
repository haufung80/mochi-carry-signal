"""Pure carry-signal logic — no IO, fully unit-testable.

Ported from the backtester (`mochi_carry_backtester/strategy.py::compute_signal`
and `display.py::{apr_to_pph,pph_to_apr}`), reduced to exactly what a *live*
signal generator needs: the trailing-average value AT `now` and a FLAT/OPEN
state-machine decision.

LOCKED signal rule (must match the backtester):
  * trailing average of ``funding_rate_pph`` over the last ``lookback_h`` hours
    of SETTLEMENTS, right-closed window ``(now - L, now]`` — NO look-ahead (a
    settlement strictly after `now`, or exactly at `now - L`, never counts);
  * OPEN  when avg >= ``apr_to_pph(10)`` (10 %/yr) AND HL spot exists;
  * CLOSE when avg <= 0.

`funding_rate_pph` is the per-hour fractional rate = ``funding_rate_native /
interval_hours`` (NO ×100) — the same unit the backtester's signal uses. The
thresholds are compared directly to that fractional per-hour rate; the
dashboard converts to annualized % only for display.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Optional, Sequence

# Linear annualization, matching the backtester's 365-day convention
# (display.HOURS_PER_YEAR = 24 * 365 = 8760).
HOURS_PER_YEAR = 24 * 365


def pph_to_apr(frac: float | None) -> float | None:
    """Fractional funding-per-hour -> annualized percent (for display)."""
    if frac is None:
        return None
    return frac * HOURS_PER_YEAR * 100.0


def apr_to_pph(apr_pct: float) -> float:
    """Annualized percent -> fractional funding-per-hour (the signal's unit)."""
    return apr_pct / 100.0 / HOURS_PER_YEAR


# Position state. CLOSE is a transition target, not a resting state; the resting
# states are FLAT and OPEN.
State = Literal["FLAT", "OPEN"]
Kind = Literal["OPEN", "CLOSE"]


@dataclass(frozen=True)
class Settlement:
    """One funding settlement: when it paid and its per-hour fractional rate."""

    time: datetime           # tz-aware UTC settlement timestamp
    funding_rate_pph: float  # native / interval_hours, fractional (no ×100)


def compute_signal(
    settlements: Sequence[Settlement],
    now: datetime,
    lookback_h: int = 72,
) -> Optional[float]:
    """Trailing average of ``funding_rate_pph`` over ``(now - lookback_h, now]``.

    Right-closed, NO look-ahead: a settlement at exactly ``now`` is included; one
    at exactly ``now - lookback_h`` or any time strictly after ``now`` is
    excluded. Returns ``None`` when the window contains no settlement (signal
    undefined -> the caller HOLDS its current state, never trades on None).

    This is the live, point-in-time form of the backtester's
    ``compute_signal`` rolling mean (``rolling("Lh", closed="right").mean()``
    over the settlement series) evaluated at a single instant ``now``.
    """
    if lookback_h <= 0:
        raise ValueError("lookback_h must be positive")
    lo = now - timedelta(hours=lookback_h)
    window = [
        s.funding_rate_pph
        for s in settlements
        if lo < s.time <= now            # right-closed (lo, now]; no look-ahead
    ]
    if not window:
        return None
    return sum(window) / len(window)


def decide(
    state: State,
    avg_pph: Optional[float],
    spot_ok: bool,
    entry_pph: float,
    exit_pph: float,
) -> Optional[Kind]:
    """The entry/exit state machine. Returns the TRANSITION to record, or None.

    FLAT  -> OPEN  when ``avg_pph >= entry_pph`` AND ``spot_ok`` (cash-and-carry
             needs a tradable HL spot leg).
    OPEN  -> CLOSE when ``avg_pph <= exit_pph``.
    Otherwise (incl. ``avg_pph is None`` => undefined signal) HOLD: return None.

    Mirrors the backtester's loop in ``run_strategy`` (FLAT->OPEN on entry,
    OPEN->FLAT on exit, NaN signal holds, spot gates the open).
    """
    if avg_pph is None:
        return None
    if state == "FLAT":
        if spot_ok and avg_pph >= entry_pph:
            return "OPEN"
        return None
    # state == "OPEN"
    if avg_pph <= exit_pph:
        return "CLOSE"
    return None
