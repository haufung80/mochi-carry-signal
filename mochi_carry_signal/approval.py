"""Approve-to-fire core (testable, independent of the HTTP route).

A pending signal is fired ONLY on the user's explicit approval:
  * pending OPEN  -> POST {PM}/funding-arb/open {idempotency_key (the signal's),
    asset, size_mode:"min", strategy_tag:"hl-cash-and-carry"} with X-Arb-Secret;
    store the returned ``arb_id``, mark ``fired``, Telegram-alert.
  * pending CLOSE -> POST {PM}/funding-arb/close {arb_id of the matching open};
    mark ``fired``, alert.
  * reject        -> mark ``rejected``, alert.

Nothing here fires automatically — these functions are invoked by the route
handlers in response to a user action. The PM HTTP call + Telegram are mocked
when ``TESTING``/``DRY_RUN`` is set (no network).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .models import Signal
from .notifier import get_notifier
from .pm_client import PMError, get_pm_client

log = logging.getLogger(__name__)


class ApprovalError(RuntimeError):
    """Raised for a bad approve/reject request (unknown id, wrong state, auth)."""


class AuthError(ApprovalError):
    """Wrong/missing APP_SECRET on an approve/reject request."""


def _check_secret(provided: str | None) -> None:
    """Gate the approve/reject action with APP_SECRET.

    Empty configured secret => gate OPEN (local dev). Otherwise the provided
    value must match exactly."""
    configured = get_settings().app_secret
    if not configured:
        return
    if provided != configured:
        raise AuthError("bad app secret")


def _get_pending(db, signal_id: int) -> Signal:
    sig = db.get(Signal, signal_id)
    if sig is None:
        raise ApprovalError(f"signal {signal_id} not found")
    if sig.status != "pending":
        raise ApprovalError(
            f"signal {signal_id} is '{sig.status}', not pending")
    return sig


def approve(signal_id: int, *, secret: str | None) -> Signal:
    """Approve + FIRE a pending signal. Returns the updated signal.

    On a PM failure the signal is marked ``error`` (with the message) and an
    alert is fired; the raised ``ApprovalError`` lets the route surface it.
    """
    _check_secret(secret)
    pm = get_pm_client()
    notifier = get_notifier()

    # Load + validate in one txn; capture what we need for the PM call.
    with session_scope() as db:
        sig = _get_pending(db, signal_id)
        asset, kind, key = sig.asset, sig.kind, sig.idempotency_key
        avg_apr = sig.trailing_avg_apr
        close_arb_id = sig.arb_id

    if kind == "CLOSE" and close_arb_id is None:
        # No arb to close — try to resolve one live from the PM before failing.
        close_arb_id = _resolve_close_arb_id(asset)

    try:
        if kind == "OPEN":
            resp = pm.open_arb(
                idempotency_key=key, asset=asset,
                size_mode=get_settings().size_mode,
                strategy_tag="hl-cash-and-carry")
            arb_id = resp.get("arb_id")
        else:  # CLOSE
            if close_arb_id is None:
                raise PMError(f"no open arb_id found to close for {asset}")
            resp = pm.close_arb(arb_id=close_arb_id)
            arb_id = close_arb_id
    except PMError as exc:
        with session_scope() as db:
            sig = db.get(Signal, signal_id)
            if sig is not None:
                sig.status = "error"
                sig.error_message = str(exc)[:1000]
        notifier.fire_error(asset, kind, str(exc))
        raise ApprovalError(str(exc)) from exc

    # Success: persist fired state + arb_id.
    with session_scope() as db:
        sig = db.get(Signal, signal_id)
        sig.status = "fired"
        sig.arb_id = int(arb_id) if arb_id is not None else sig.arb_id
        sig.fired_at = datetime.now(timezone.utc)
        sig.error_message = ""
        updated = _snapshot(sig)

    if kind == "OPEN":
        notifier.opened(asset, updated.arb_id or -1, avg_apr)
    else:
        notifier.closed(asset, updated.arb_id or -1)
    log.info("fired %s for %s -> arb_id=%s", kind, asset, updated.arb_id)
    return updated


def reject(signal_id: int, *, secret: str | None) -> Signal:
    """Reject a pending signal (no order sent). Returns the updated signal."""
    _check_secret(secret)
    with session_scope() as db:
        sig = _get_pending(db, signal_id)
        sig.status = "rejected"
        asset, kind = sig.asset, sig.kind
        updated = _snapshot(sig)
    get_notifier().rejected(asset, kind)
    log.info("rejected %s for %s", kind, asset)
    return updated


def _resolve_close_arb_id(asset: str) -> int | None:
    """Find a live PM open arb_id for ``asset`` (used when our signal lacks one)."""
    for pos in get_pm_client().positions():
        if (pos.get("asset") or "").upper() == asset.upper() and \
                (pos.get("status") or "").lower() != "closed":
            aid = pos.get("arb_id")
            if aid is not None:
                return int(aid)
    return None


def _snapshot(sig: Signal) -> Signal:
    """Detached copy of a Signal's display fields for post-commit use."""
    return Signal(
        id=sig.id, asset=sig.asset, kind=sig.kind,
        trailing_avg_pph=sig.trailing_avg_pph,
        trailing_avg_apr=sig.trailing_avg_apr,
        funding_now_apr=sig.funding_now_apr,
        spot_available=sig.spot_available, status=sig.status,
        idempotency_key=sig.idempotency_key, arb_id=sig.arb_id,
        fired_at=sig.fired_at, created_at=sig.created_at,
        error_message=sig.error_message,
    )
