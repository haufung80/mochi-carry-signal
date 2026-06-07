"""Pure funding-chart builder tests (no IO) — like signal.py's unit tests.

Asserts ``build_funding_chart``'s geometry AND that its "trailing funding" line is
the SAME LOCKED rule the poller acts on (``signal.compute_signal``), and that
OPEN/CLOSE signals turn into in-window chart markers.
"""
import math
from datetime import datetime, timedelta, timezone

import pytest

from mochi_carry_signal.chart import (
    _MAX_DRAW_POINTS,
    _trailing_apr_series,
    SignalEvent,
    build_funding_chart,
)
from mochi_carry_signal.signal import Settlement, compute_signal, pph_to_apr

NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


def _setts(native_rates, *, base=NOW, step_h=1):
    """Hourly settlements ending at ``base`` from native per-hour rates."""
    n = len(native_rates)
    return [Settlement(time=base - timedelta(hours=step_h * (n - 1 - i)),
                       funding_rate_pph=native / step_h)
            for i, native in enumerate(native_rates)]


def _coords(polyline):
    return [tuple(map(float, p.split(","))) for p in polyline.split()] \
        if polyline else []


def _build(setts, signals=(), *, chart_days=30, entry=10.0, exit_=0.0):
    return build_funding_chart("BTC", setts, list(signals), NOW,
                               lookback_h=72, chart_days=chart_days,
                               entry_apr=entry, exit_apr=exit_)


# --------------------------------------------------------------------------- #
# Empty / degenerate
# --------------------------------------------------------------------------- #

def test_empty_settlements_returns_none():
    assert _build([]) is None


def test_settlements_all_before_window_return_none():
    # Settlements that end 40 days ago: nothing falls in the 30-day window.
    old = _setts([1e-4] * 24, base=NOW - timedelta(days=40))
    assert _build(old) is None


# --------------------------------------------------------------------------- #
# Series geometry
# --------------------------------------------------------------------------- #

def test_window_point_counts_and_alignment():
    setts = _setts([1e-4] * (35 * 24))          # 35 days hourly
    c = _build(setts)
    # Right-closed 30-day window of hourly points, both ends inclusive.
    assert c.point_count == 30 * 24 + 1
    assert len(_coords(c.raw_polyline)) == c.point_count
    assert len(_coords(c.trail_polyline)) == c.point_count   # trailing never None in-window


def test_x_coords_increase_with_time():
    c = _build(_setts([1e-4] * (31 * 24)))
    xs = [x for x, _ in _coords(c.trail_polyline)]
    assert xs == sorted(xs)
    assert xs[0] >= c.plot_left and xs[-1] <= c.plot_right
    assert len(c.x_ticks) == 5
    assert len(c.y_ticks) >= 2


def test_all_drawn_points_within_plot_box():
    c = _build(_setts([2e-5 + 4e-5 * (i / 500) for i in range(31 * 24)]))
    for poly in (c.raw_polyline, c.trail_polyline):
        for _, y in _coords(poly):
            assert c.plot_top - 0.6 <= y <= c.plot_bottom + 0.6


# --------------------------------------------------------------------------- #
# The trailing line IS the LOCKED rule (no second implementation)
# --------------------------------------------------------------------------- #

def test_last_trailing_value_matches_compute_signal():
    rates = [2e-5 + 5e-5 * (i / (33 * 24)) for i in range(33 * 24)]   # ramps up
    setts = _setts(rates)
    c = _build(setts)
    disp = sorted((s for s in setts
                   if NOW - timedelta(days=30) <= s.time <= NOW),
                  key=lambda s: s.time)
    expected = pph_to_apr(compute_signal(setts, disp[-1].time, 72))
    assert c.last_trail_apr == pytest.approx(expected)


def test_constant_funding_gives_flat_trailing_line():
    rate = 1e-4
    c = _build(_setts([rate] * (31 * 24)))
    ys = [y for _, y in _coords(c.trail_polyline)]
    assert max(ys) - min(ys) < 1e-6                       # flat
    assert c.last_trail_apr == pytest.approx(pph_to_apr(rate))


# --------------------------------------------------------------------------- #
# Entry/exit markers
# --------------------------------------------------------------------------- #

def test_signals_become_markers_only_within_window():
    sigs = [
        SignalEvent(NOW - timedelta(days=5), "OPEN", 15.0, "fired"),
        SignalEvent(NOW - timedelta(days=1), "CLOSE", -0.5, "pending"),
        SignalEvent(NOW - timedelta(days=45), "OPEN", 11.0, "fired"),   # out of window
    ]
    c = _build(_setts([1e-4] * (31 * 24)), sigs)
    assert len(c.markers) == 2
    open_m = next(m for m in c.markers if m.kind == "OPEN")
    close_m = next(m for m in c.markers if m.kind == "CLOSE")

    assert open_m.color == "#4ade80" and open_m.filled is True     # fired -> solid
    assert close_m.color == "#f87171" and close_m.filled is False  # pending -> hollow
    assert open_m.cx < close_m.cx                                   # earlier -> left
    for m in c.markers:
        assert c.plot_left <= m.cx <= c.plot_right
        assert c.plot_top - 6 <= m.cy <= c.plot_bottom + 6
        assert len(m.points.split()) == 3                          # a triangle


def test_rejected_marker_is_faded_and_hollow():
    sigs = [SignalEvent(NOW - timedelta(days=3), "OPEN", 12.0, "rejected")]
    c = _build(_setts([1e-4] * (31 * 24)), sigs)
    assert c.markers[0].opacity < 1.0
    assert c.markers[0].filled is False


# --------------------------------------------------------------------------- #
# Scale robustness: a raw spike must not squash the signal line off-chart
# --------------------------------------------------------------------------- #

def test_entry_line_visible_and_raw_spike_clamped_to_scale():
    rates = [3e-5] * (31 * 24)
    rates[100] = 5e-3                              # one ~4380%/yr one-hour spike
    c = _build(_setts(rates))
    assert c.entry_y is not None                   # 10%/yr entry stays on-scale
    assert c.zero_y is not None
    # the spike is clamped onto the plot, not allowed to blow out the y-domain
    assert all(c.plot_top - 0.6 <= y <= c.plot_bottom + 0.6
               for _, y in _coords(c.raw_polyline))


# --------------------------------------------------------------------------- #
# 6-month window: O(N) trailing stays faithful, and the drawn path is strided
# --------------------------------------------------------------------------- #

def test_rolling_trailing_equals_locked_compute_signal():
    # The windowed O(N) trailing must be IDENTICAL to rescanning all settlements
    # with the LOCKED compute_signal (it just slices the window first).
    rates = [2e-5 + 5e-5 * math.sin(i / 13.0) for i in range(40 * 24)]
    all_sorted = sorted(_setts(rates), key=lambda s: s.time)
    disp = [s for s in all_sorted if NOW - timedelta(days=30) <= s.time <= NOW]
    fast = _trailing_apr_series(all_sorted, disp, 72)
    slow = [pph_to_apr(compute_signal(all_sorted, s.time, 72)) for s in disp]
    assert fast == slow


def test_six_month_window_downsamples_drawn_path_keeping_span():
    c = _build(_setts([2e-5] * (180 * 24 + 72)), chart_days=180)
    assert c.point_count == 180 * 24 + 1               # every settlement is data...
    raw = _coords(c.raw_polyline)
    assert len(raw) < c.point_count                    # ...but the path is strided
    assert len(raw) <= _MAX_DRAW_POINTS + 1
    xs = [x for x, _ in raw]
    assert xs[0] <= c.plot_left + 1                     # first + last kept -> full span
    assert xs[-1] >= c.plot_right - 1
