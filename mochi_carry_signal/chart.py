"""Pure SVG funding-chart builder — no IO, fully unit-testable (like ``signal.py``).

Per asset, this renders ~1 month of HISTORICAL funding (annualized %) with its
trailing-``lookback_h`` average overlaid, the entry/exit threshold lines, and
markers at the times OPEN/CLOSE signals fired. The output is plain coordinates
the template draws as inline SVG: **zero JS, zero CDN**, so it works in the
dashboard's offline/dry-run mode (no external chart library to fetch).

Units: the engine works in fractional per-hour (pph); the chart — like the rest
of the dashboard — displays ANNUALIZED PERCENT via ``signal.pph_to_apr``.

The trailing line is computed with the LOCKED ``signal.compute_signal`` evaluated
at each settlement instant, so the chart's "trailing funding" is exactly the
signal the poller acts on (no second implementation to drift).
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from .signal import Settlement, compute_signal, pph_to_apr

# Marker colours (match the dashboard's .pos/.neg palette).
_OPEN_COLOR = "#4ade80"   # green ▲
_CLOSE_COLOR = "#f87171"  # red ▼

# Cap on points actually DRAWN per line. A months-long hourly window is otherwise
# thousands of sub-pixel points (heavy HTML, no visual gain); the trailing average
# still uses EVERY settlement — only the emitted polyline is strided.
_MAX_DRAW_POINTS = 1600
# Cap on points emitted for the hover/tap tooltip (coarser than the drawn line —
# the crosshair snaps to the nearest, sub-pixel resolution isn't needed).
_MAX_TOOLTIP_POINTS = 360


@dataclass(frozen=True)
class SignalEvent:
    """A recorded signal to mark on the chart (a thin view of a ``Signal`` row)."""

    time: datetime            # tz-aware UTC
    kind: str                 # "OPEN" | "CLOSE"
    trailing_avg_apr: float   # the trailing avg at firing (the marker's y)
    status: str               # pending|approved|fired|rejected|error


@dataclass(frozen=True)
class Marker:
    cx: float
    cy: float
    kind: str                 # OPEN | CLOSE
    color: str
    points: str               # SVG triangle polygon points
    filled: bool              # fired/approved => solid; else hollow outline
    opacity: float            # rejected/error => faded
    title: str                # native <title> hover tooltip


@dataclass(frozen=True)
class Tick:
    pos: float                # svg coord (x for x-ticks, y for y-ticks)
    label: str


@dataclass(frozen=True)
class ChartView:
    asset: str
    width: int
    height: int
    plot_left: float
    plot_right: float
    plot_top: float
    plot_bottom: float
    raw_polyline: str         # per-settlement funding (annualized %), clamped to scale
    trail_polyline: str       # trailing-lookback_h average (annualized %)
    trail_area: str           # polygon: trailing line down to the baseline (fill)
    entry_y: Optional[float]  # y of the entry threshold (None if off-scale)
    zero_y: Optional[float]   # y of the exit/zero threshold (None if off-scale)
    markers: list             # list[Marker]
    x_ticks: list             # list[Tick]
    y_ticks: list             # list[Tick]
    last_raw_apr: Optional[float]
    last_trail_apr: Optional[float]
    point_count: int
    # Coarse per-point data for the hover/tap tooltip (true, UNCLAMPED values):
    # [x_px, raw_apr, trail_apr|None, epoch_seconds, trail_y_px|None], x-sorted.
    tooltip_points: list


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted sequence."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_vals[int(idx)]
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _nice_step(span: float, target_ticks: int = 4) -> float:
    """A human-friendly axis step (1/2/2.5/5 × 10^k) covering ``span``."""
    if span <= 0:
        return 1.0
    raw = span / max(target_ticks, 1)
    mag = 10.0 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _fmt_pct(v: float, step: float) -> str:
    decimals = 0 if step >= 1 else (1 if step >= 0.1 else 2)
    # Avoid "-0%".
    if abs(v) < step / 1000:
        v = 0.0
    return f"{v:.{decimals}f}%"


def _trailing_apr_series(all_sorted, query_pts, lookback_h):
    """Trailing-avg APR at each query point — the LOCKED rule, O(N) not O(N^2).

    For each point it bisects the ``(t-L, t]`` window out of the time-sorted
    settlements and hands the slice to ``compute_signal`` (which re-applies the
    exact right-closed filter), so compute_signal stays the single source of truth
    — without rescanning every settlement for every point (the difference between
    ~20 ms and ~500 ms once the window is months long)."""
    times = [s.time.timestamp() for s in all_sorted]
    win_s = lookback_h * 3600.0
    out = []
    for q in query_pts:
        t = q.time.timestamp()
        lo = bisect.bisect_left(times, t - win_s)   # generous; compute_signal re-filters
        hi = bisect.bisect_right(times, t)
        out.append(pph_to_apr(compute_signal(all_sorted[lo:hi], q.time, lookback_h)))
    return out


def _draw_indices(n: int, max_points: int):
    """Indices to actually draw: all of them, or a uniform stride that always
    keeps the first and last point (so the full time span is still spanned)."""
    if n <= max_points:
        return range(n)
    stride = math.ceil(n / max_points)
    idx = list(range(0, n, stride))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return idx


def build_funding_chart(
    asset: str,
    settlements: Sequence[Settlement],
    signals: Sequence[SignalEvent],
    now: datetime,
    *,
    lookback_h: int,
    chart_days: int,
    entry_apr: float,
    exit_apr: float,
    width: int = 720,
    height: int = 240,
    pad_left: int = 48,
    pad_right: int = 14,
    pad_top: int = 12,
    pad_bottom: int = 26,
) -> Optional[ChartView]:
    """Build a ``ChartView`` for one asset, or ``None`` if there's nothing to show.

    ``settlements`` should span ``chart_days`` PLUS ``lookback_h`` hours of history
    so the trailing line is correct from the first displayed day. Only settlements
    within ``(now - chart_days, now]`` are drawn; the rest feed the trailing avg.
    """
    display_start = now - timedelta(days=chart_days)
    pts = sorted((s for s in settlements if display_start <= s.time <= now),
                 key=lambda s: s.time)
    if not pts:
        return None

    all_sorted = sorted(settlements, key=lambda s: s.time)
    raw_apr = [pph_to_apr(s.funding_rate_pph) for s in pts]
    # Trailing avg AT each displayed settlement via the LOCKED rule (self always
    # falls inside its own right-closed window, so this is never None here). O(N)
    # windowed eval so a 6-month window stays fast.
    trail_apr = _trailing_apr_series(all_sorted, pts, lookback_h)

    in_window = [ev for ev in signals if display_start <= ev.time <= now]

    # --- y-domain: driven by the trailing line (the signal), thresholds and
    # markers, plus a ROBUST raw range (2nd–98th pct). Spiky per-settlement
    # funding is then clamped onto the scale so it can't squash the trailing line.
    trail_vals = [v for v in trail_apr if v is not None]
    domain_vals = list(trail_vals) + [entry_apr, exit_apr]
    domain_vals += [ev.trailing_avg_apr for ev in in_window]
    if raw_apr:
        rs = sorted(raw_apr)
        domain_vals += [_percentile(rs, 0.02), _percentile(rs, 0.98)]
    vmin, vmax = min(domain_vals), max(domain_vals)
    if vmax - vmin < 1e-9:
        vmin -= 1.0
        vmax += 1.0
    span = vmax - vmin
    vmin -= span * 0.08
    vmax += span * 0.08

    # --- coordinate transforms ---
    plot_left = float(pad_left)
    plot_right = float(width - pad_right)
    plot_top = float(pad_top)
    plot_bottom = float(height - pad_bottom)
    plot_w = plot_right - plot_left
    plot_h = plot_bottom - plot_top

    t0 = display_start.timestamp()
    t1 = now.timestamp()
    if t1 <= t0:
        t1 = t0 + 1.0

    def X(ts: float) -> float:
        return plot_left + (ts - t0) / (t1 - t0) * plot_w

    def Y(v: float) -> float:
        return plot_top + (vmax - v) / (vmax - vmin) * plot_h

    def _clamp(v: float) -> float:
        return min(max(v, vmin), vmax)

    xs = [X(s.time.timestamp()) for s in pts]
    draw = _draw_indices(len(pts), _MAX_DRAW_POINTS)
    raw_polyline = " ".join(
        f"{xs[i]:.1f},{Y(_clamp(raw_apr[i])):.1f}" for i in draw)
    trail_xy = [(xs[i], Y(trail_apr[i])) for i in draw if trail_apr[i] is not None]
    trail_polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in trail_xy)
    trail_area = ""
    if trail_xy:
        trail_area = (trail_polyline
                      + f" {trail_xy[-1][0]:.1f},{plot_bottom:.1f}"
                      + f" {trail_xy[0][0]:.1f},{plot_bottom:.1f}")

    # Coarse per-point data for the hover/tap tooltip — TRUE (unclamped) funding.
    tt_idx = _draw_indices(len(pts), _MAX_TOOLTIP_POINTS)
    tooltip_points = [
        [round(xs[i], 1), round(raw_apr[i], 1),
         (round(trail_apr[i], 1) if trail_apr[i] is not None else None),
         int(pts[i].time.timestamp()),
         (round(Y(trail_apr[i]), 1) if trail_apr[i] is not None else None)]
        for i in tt_idx
    ]

    def _visible_y(v: float) -> Optional[float]:
        y = Y(v)
        return y if plot_top - 0.5 <= y <= plot_bottom + 0.5 else None

    entry_y = _visible_y(entry_apr)
    zero_y = _visible_y(exit_apr)

    # --- markers ---
    markers: list[Marker] = []
    r = 5.0
    for ev in in_window:
        cx = X(ev.time.timestamp())
        cy = Y(ev.trailing_avg_apr)
        is_open = ev.kind.upper() == "OPEN"
        color = _OPEN_COLOR if is_open else _CLOSE_COLOR
        if is_open:  # up triangle
            pts_str = (f"{cx:.1f},{cy - r:.1f} "
                       f"{cx - r:.1f},{cy + r:.1f} {cx + r:.1f},{cy + r:.1f}")
        else:        # down triangle
            pts_str = (f"{cx:.1f},{cy + r:.1f} "
                       f"{cx - r:.1f},{cy - r:.1f} {cx + r:.1f},{cy - r:.1f}")
        markers.append(Marker(
            cx=round(cx, 1), cy=round(cy, 1), kind=ev.kind.upper(), color=color,
            points=pts_str,
            filled=ev.status in ("fired", "approved"),
            opacity=(0.45 if ev.status in ("rejected", "error") else 1.0),
            title=(f"{ev.kind.upper()} · {ev.time:%Y-%m-%d %H:%M} · "
                   f"{ev.trailing_avg_apr:+.2f}%/yr · {ev.status}")))

    # --- axis ticks ---
    step = _nice_step(vmax - vmin, target_ticks=4)
    y_ticks: list[Tick] = []
    v = math.ceil(vmin / step) * step
    while v <= vmax + 1e-9:
        y_ticks.append(Tick(pos=round(Y(v), 1), label=_fmt_pct(v, step)))
        v += step

    x_ticks: list[Tick] = []
    for i in range(5):
        ts = t0 + (i / 4) * (t1 - t0)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        x_ticks.append(Tick(pos=round(X(ts), 1), label=dt.strftime("%m-%d")))

    return ChartView(
        asset=asset.upper(), width=width, height=height,
        plot_left=plot_left, plot_right=plot_right,
        plot_top=plot_top, plot_bottom=plot_bottom,
        raw_polyline=raw_polyline, trail_polyline=trail_polyline,
        trail_area=trail_area, entry_y=entry_y, zero_y=zero_y,
        markers=markers, x_ticks=x_ticks, y_ticks=y_ticks,
        last_raw_apr=raw_apr[-1] if raw_apr else None,
        last_trail_apr=trail_apr[-1] if trail_apr else None,
        point_count=len(pts), tooltip_points=tooltip_points)
