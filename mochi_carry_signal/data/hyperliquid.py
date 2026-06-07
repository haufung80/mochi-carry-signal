"""Minimal Hyperliquid public-market data: live funding + spot availability.

A small VENDORED copy of the backtester's HL seam
(``mochi_carry_backtester/data/hyperliquid.py``): the public ``/info`` POST
endpoint (no auth), a paginated ``fetch_funding``, per-hour normalization
(``funding_rate_pph = funding_rate_native / interval_hours``), and a
``has_spot(asset)`` check (does an HL Unit spot market exist for the coin via
``spotMeta``).

``_post`` is the single HTTP seam — tests monkeypatch it to run offline. We
DON'T pull in the whole backtester package; only this thin slice is needed.

Funding/interval conventions kept identical to the backtester so the signal
matches it bit-for-bit:
  * ``funding_rate_native`` is HL's per-interval fractional rate.
  * The interval is inferred per-settlement from timestamp deltas; HL funding is
    hourly, so the interval is ~1h and ``pph ≈ native`` — but we infer it rather
    than hard-code so a venue cadence change can't silently mis-scale the signal.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)

INFO_URL = "https://api.hyperliquid.xyz/info"

_FUNDING_PAGE = 500
_MAX_ATTEMPTS = 5
_BASE_DELAY = 1.0
_MAX_DELAY = 30.0
_DEFAULT_INTERVAL_HOURS = 1.0     # HL funding settles hourly


@dataclass(frozen=True)
class FundingPoint:
    """One normalized funding settlement (matches signal.Settlement's fields)."""

    time: datetime                # tz-aware UTC
    funding_rate_native: float    # HL per-interval fractional rate
    interval_hours: float         # inferred gap to the previous settlement (h)
    funding_rate_pph: float       # native / interval_hours (fractional, no ×100)


def _post(body: dict):
    """POST ``body`` to the HL info endpoint with retry/backoff; returns JSON.

    The ONE network seam — monkeypatched in tests so the suite never hits the
    network. Retries any transient failure with exponential backoff (no
    tenacity, matching the backtester)."""
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(INFO_URL, json=body)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — retry any transient failure
            last_exc = exc
            if attempt == _MAX_ATTEMPTS:
                break
            delay = min(_BASE_DELAY * (3 ** (attempt - 1)), _MAX_DELAY)
            log.warning("HL POST %s attempt %d failed: %s; retry in %.1fs",
                        body.get("type"), attempt, exc, delay)
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _raw_funding(asset: str, start_ms: int, end_ms: int) -> list[dict]:
    """Paginated raw fundingHistory rows for ``asset`` over [start_ms, end_ms].

    Paginates to ``end_ms`` (never stops on a short page mid-range — that
    truncation bug is called out in the backtester) and dedups/sorts by time.
    """
    coin = asset.upper()
    rows: list[dict] = []
    start = start_ms
    while True:
        batch = _post({"type": "fundingHistory", "coin": coin,
                       "startTime": start, "endTime": end_ms}) or []
        if not batch:
            break
        rows.extend(batch)
        last_t = max(int(x["time"]) for x in batch)
        if last_t >= end_ms or len(batch) < _FUNDING_PAGE:
            break
        start = last_t + 1
    # Dedup + sort ascending by settlement time.
    by_time: dict[int, dict] = {}
    for r in rows:
        by_time[int(r["time"])] = r
    return [by_time[t] for t in sorted(by_time)]


def fetch_funding(asset: str, start_ms: int, end_ms: int) -> list[FundingPoint]:
    """Fetch + normalize recent HL funding for ``asset`` into ``FundingPoint``s.

    Per-hour rate ``funding_rate_pph = funding_rate_native / interval_hours``
    where ``interval_hours`` is the gap (hours) to the previous settlement (the
    first row uses the venue default of 1h). This is exactly the backtester's
    loader convention, so the trailing-average signal computed downstream
    matches the backtest.
    """
    raw = _raw_funding(asset, start_ms, end_ms)
    points: list[FundingPoint] = []
    prev_ms: Optional[int] = None
    for r in raw:
        t_ms = int(r["time"])
        native = float(r["fundingRate"])
        if prev_ms is None:
            interval_h = _DEFAULT_INTERVAL_HOURS
        else:
            interval_h = (t_ms - prev_ms) / 3_600_000.0
            if interval_h <= 0:
                interval_h = _DEFAULT_INTERVAL_HOURS
        prev_ms = t_ms
        points.append(FundingPoint(
            time=datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc),
            funding_rate_native=native,
            interval_hours=interval_h,
            funding_rate_pph=native / interval_h,
        ))
    return points


def _resolve_spot_coin(asset: str) -> Optional[str]:
    """Map BTC/ETH/SOL -> an HL spot-market name via ``spotMeta``.

    HL Unit tokens are prefixed 'U' (e.g. UBTC), so we accept either the bare
    coin or its Unit-bridged name as the spot base token. Returns the spot pair
    name (e.g. ``@142`` / ``UBTC/USDC``) or None when no such market exists.
    Ported from the backtester's ``_resolve_spot_coin``.
    """
    meta = _post({"type": "spotMeta"}) or {}
    tokens = {int(t["index"]): t["name"] for t in meta.get("tokens", [])}
    wanted = {asset.upper(), "U" + asset.upper()}
    for pair in meta.get("universe", []):
        token_idxs = pair.get("tokens") or []
        if token_idxs and tokens.get(int(token_idxs[0]), "") in wanted:
            return pair.get("name")
    return None


def has_spot(asset: str) -> bool:
    """Does a tradable HL spot market exist for ``asset``?

    The cash-and-carry signal may only OPEN where spot is tradable (the spot
    gate). Best-effort: any resolution failure is treated as "no spot" so we
    never emit an un-hedgeable OPEN on a transient error. Never raises."""
    try:
        return _resolve_spot_coin(asset) is not None
    except Exception as exc:  # noqa: BLE001 — gate must never raise into the poller
        log.warning("HL spot resolution failed for %s: %s; treating as no-spot",
                    asset, exc)
        return False
