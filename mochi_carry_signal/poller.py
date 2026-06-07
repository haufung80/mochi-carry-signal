"""Background hourly poller: fetch HL funding -> compute signal -> RECORD.

For each configured asset (BTC/ETH/SOL):
  1. fetch recent HL funding + spot availability;
  2. compute the trailing-72h ``funding_rate_pph`` average at ``now`` (the
     LOCKED rule, no look-ahead) + the current funding rate;
  3. derive the asset's CURRENT resting state (FLAT/OPEN) from the latest
     non-rejected ``Signal`` AND live PM open arbs;
  4. run the FLAT/OPEN state machine; on a state CHANGE, insert ONE *pending*
     ``Signal`` and fire a Telegram alert.

Idempotent: a repeat poll in the same state inserts nothing. Dedup is via a
deterministic ``idempotency_key`` per (asset, transition, hour) + the UNIQUE
constraint on the column — a duplicate insert is swallowed.

It NEVER fires an order; recording is automatic, firing is approve-to-fire (see
``approval.py``). The loop sleeps FIRST so app startup makes no network calls
(mirrors the position-manager's funding_worker).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import Settings, get_settings
from .data import hyperliquid as hl
from .db import session_scope
from .models import Signal
from .notifier import get_notifier
from .pm_client import get_pm_client
from .signal import State, apr_to_pph, compute_signal, decide, pph_to_apr
from .signal import Settlement

log = logging.getLogger(__name__)

# Re-scan a generous window each poll; the trailing-72h average only needs the
# last LOOKBACK hours of settlements, but a wider fetch tolerates gaps/cadence.
_FETCH_LOOKBACK_HOURS = 24 * 14        # 14 days of funding history per poll

# A signal still "holds" the OPEN state for every status except an explicit
# reject (a pending/approved/fired OPEN means we believe we're OPEN; an errored
# OPEN we also treat as OPEN so we don't re-fire on top of a half-open arb).
_OPEN_HOLDING = ("pending", "approved", "fired", "error")


def idempotency_key(asset: str, kind: str, now: datetime) -> str:
    """Deterministic key per (asset, transition, HOUR).

    Hour granularity matches the hourly poll: two polls in the same clock hour
    that both see the same transition produce the SAME key, so the UNIQUE
    constraint dedups them. This is ALSO the key sent to the PM /open.
    Format mirrors the PM's example: ``sig-<ISO-hour>Z-<ASSET>-<KIND>``.
    """
    hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    stamp = hour.strftime("%Y-%m-%dT%H:00:00")
    return f"sig-{stamp}Z-{asset.upper()}-{kind.upper()}"


def _latest_signal(db, asset: str) -> Signal | None:
    return db.execute(
        select(Signal).where(Signal.asset == asset)
        .order_by(Signal.created_at.desc(), Signal.id.desc()).limit(1)
    ).scalars().first()


def _pm_open_assets(pm) -> set[str]:
    """Assets with a live, non-closed arb on the PM (truth for the OPEN state).

    Best-effort: a PM read failure returns an empty set, so state derivation
    falls back to our own signal log only.
    """
    open_assets: set[str] = set()
    for pos in pm.positions():
        status = (pos.get("status") or "").lower()
        if status not in ("closed",):
            asset = (pos.get("asset") or "").upper()
            if asset:
                open_assets.add(asset)
    return open_assets


def _derive_state(db, pm_open: set[str], asset: str) -> State:
    """Current resting state for ``asset`` from (PM open arbs) ∪ (our signal log).

    OPEN iff the PM reports a non-closed arb for the asset, OR our latest
    non-rejected signal for the asset is an OPEN that hasn't been superseded by
    a CLOSE. Otherwise FLAT. Combining both sources keeps us correct even if one
    is briefly unavailable (PM unreachable) or stale (a CLOSE not yet fired)."""
    if asset.upper() in pm_open:
        return "OPEN"
    last = _latest_signal(db, asset)
    if last is None:
        return "FLAT"
    # Walk back to the most recent signal that actually defines state: a
    # non-rejected OPEN => OPEN; a fired/holding CLOSE => FLAT; skip rejects.
    if last.status == "rejected":
        # Find the most recent non-rejected signal instead.
        prior = db.execute(
            select(Signal).where(Signal.asset == asset,
                                 Signal.status != "rejected")
            .order_by(Signal.created_at.desc(), Signal.id.desc()).limit(1)
        ).scalars().first()
        last = prior
    if last is None:
        return "FLAT"
    if last.kind == "OPEN" and last.status in _OPEN_HOLDING:
        return "OPEN"
    return "FLAT"


def _arb_id_for_close(db, pm_open_positions: list[dict], asset: str) -> int | None:
    """Best-effort arb_id to close for ``asset``: prefer a live PM open arb,
    else the arb_id stored on our last fired OPEN signal."""
    for pos in pm_open_positions:
        if (pos.get("asset") or "").upper() == asset.upper() and \
                (pos.get("status") or "").lower() != "closed":
            aid = pos.get("arb_id")
            if aid is not None:
                return int(aid)
    last_open = db.execute(
        select(Signal).where(Signal.asset == asset, Signal.kind == "OPEN",
                             Signal.arb_id.isnot(None))
        .order_by(Signal.created_at.desc(), Signal.id.desc()).limit(1)
    ).scalars().first()
    return last_open.arb_id if last_open else None


def poll_asset(asset: str, now: datetime, settings: Settings,
               pm_open: set[str], pm_positions: list[dict]) -> Signal | None:
    """Poll one asset; insert + return a pending ``Signal`` on a transition, else None.

    Resilient: an HL fetch failure for this asset logs and returns None (the
    loop continues with the other assets)."""
    entry_pph = apr_to_pph(settings.entry_apr)
    exit_pph = apr_to_pph(settings.exit_apr)
    now_ms = int(now.timestamp() * 1000)
    start_ms = now_ms - _FETCH_LOOKBACK_HOURS * 3_600_000

    try:
        points = hl.fetch_funding(asset, start_ms, now_ms)
        spot_ok = hl.has_spot(asset)
    except Exception:
        log.exception("HL fetch failed for %s; skipping this poll", asset)
        return None

    settlements = [Settlement(time=p.time, funding_rate_pph=p.funding_rate_pph)
                   for p in points]
    avg_pph = compute_signal(settlements, now, settings.lookback_hours)
    funding_now_pph = points[-1].funding_rate_pph if points else None

    with session_scope() as db:
        state = _derive_state(db, pm_open, asset)
        kind = decide(state, avg_pph, spot_ok, entry_pph, exit_pph)
        if kind is None:
            return None  # hold — nothing to record

        key = idempotency_key(asset, kind, now)
        # Pre-check (cheap) THEN rely on the UNIQUE constraint as the hard gate.
        existing = db.execute(
            select(Signal).where(Signal.idempotency_key == key)
        ).scalars().first()
        if existing is not None:
            log.debug("signal %s already recorded (idempotent no-op)", key)
            return None

        arb_id = None
        if kind == "CLOSE":
            arb_id = _arb_id_for_close(db, pm_positions, asset)

        sig = Signal(
            created_at=now,
            asset=asset.upper(),
            kind=kind,
            trailing_avg_pph=float(avg_pph),
            trailing_avg_apr=float(pph_to_apr(avg_pph)),
            funding_now_apr=(None if funding_now_pph is None
                             else float(pph_to_apr(funding_now_pph))),
            spot_available=bool(spot_ok),
            status="pending",
            idempotency_key=key,
            arb_id=arb_id,
        )
        db.add(sig)
        try:
            db.flush()
        except IntegrityError:
            # Lost a race on the UNIQUE key — another poll recorded it first.
            db.rollback()
            log.debug("signal %s lost insert race (idempotent no-op)", key)
            return None
        # Snapshot fields for the post-commit notify (avoid touching a detached
        # instance after session close).
        recorded = Signal(
            id=sig.id, asset=sig.asset, kind=sig.kind,
            trailing_avg_apr=sig.trailing_avg_apr,
            funding_now_apr=sig.funding_now_apr,
            trailing_avg_pph=sig.trailing_avg_pph,
            spot_available=sig.spot_available,
            status=sig.status, idempotency_key=sig.idempotency_key,
            arb_id=sig.arb_id, created_at=sig.created_at,
        )

    # Best-effort alert AFTER the row is committed (outside session_scope).
    get_notifier().signal_generated(
        recorded.asset, recorded.kind, recorded.trailing_avg_apr,
        recorded.funding_now_apr)
    log.info("recorded %s signal for %s (avg=%.2f%%/yr, spot=%s) -> pending",
             recorded.kind, recorded.asset, recorded.trailing_avg_apr,
             recorded.spot_available)
    return recorded


def poll_once(now: datetime | None = None,
              settings: Settings | None = None) -> list[Signal]:
    """One full poll across all configured assets. Returns the new signals.

    Reads PM open positions ONCE (shared across assets) then polls each asset.
    Resilient per asset."""
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    pm = get_pm_client()
    pm_positions = pm.positions()
    pm_open = {(p.get("asset") or "").upper() for p in pm_positions
               if (p.get("status") or "").lower() != "closed"}
    pm_open.discard("")

    recorded: list[Signal] = []
    for asset in settings.assets:
        sig = poll_asset(asset, now, settings, pm_open, pm_positions)
        if sig is not None:
            recorded.append(sig)
    return recorded


async def poll_loop(*, poll_seconds: float | None = None,
                    stop_event: asyncio.Event | None = None) -> None:
    """Periodic poll. Sleeps FIRST (network-free startup), blocking work in a
    thread so the event loop stays free. Mirrors the PM's funding_loop."""
    settings = get_settings()
    interval = poll_seconds if poll_seconds is not None else settings.poll_seconds
    log.info("poller started (poll=%.0fs, first scan after one interval)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("poller cancelled")
            return
        if stop_event is not None and stop_event.is_set():
            log.info("poller stopping (stop_event set)")
            return
        try:
            new = await asyncio.to_thread(poll_once)
            if new:
                log.info("poller: recorded %d new signal(s)", len(new))
        except Exception:
            log.exception("poller loop error")
