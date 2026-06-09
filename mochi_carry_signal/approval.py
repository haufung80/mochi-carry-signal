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

import hmac
import logging
import re
import time
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
    """Gate approve/reject/retry with APP_SECRET (constant-time compare).

    An empty configured secret opens the gate ONLY in dev (TESTING/DRY_RUN); in
    production (a public deploy) it FAILS CLOSED — actions are denied — so the
    site can never fire wide-open through a missing-secret misconfiguration."""
    s = get_settings()
    configured = s.app_secret
    if not configured:
        if s.offline:            # local dev / tests only
            return
        raise AuthError("actions are disabled: APP_SECRET is not set")
    # Constant-time comparison to avoid leaking the secret via timing.
    if not hmac.compare_digest(provided or "", configured):
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


# A signal is retryable when its OPEN was fired but the arb failed to execute
# (PM arb status 'error'), or the fire request itself errored.
_RETRYABLE_STATUSES = ("fired", "error")


def retry(signal_id: int, *, secret: str | None) -> Signal:
    """Re-attempt a FAILED open: close the stuck arb, then fire a FRESH open.

    For an OPEN signal whose arb failed to execute (or whose fire errored). It:
      1. refuses if the asset's arb is actually healthy/in-flight (no churn);
      2. closes the old/failed arb to free the symbol (best-effort) and waits
         briefly for the PM's async close to clear it;
      3. re-opens with a NEW idempotency_key (the old key dedups to the dead
         arb), marking the signal ``fired`` on success.
    Gated by APP_SECRET. If the old arb is still closing, raises a friendly
    transient error (the route surfaces "click Retry again in a few seconds").
    """
    _check_secret(secret)
    pm = get_pm_client()
    notifier = get_notifier()

    with session_scope() as db:
        sig = db.get(Signal, signal_id)
        if sig is None:
            raise ApprovalError(f"signal {signal_id} not found")
        if sig.kind != "OPEN" or sig.status not in _RETRYABLE_STATUSES:
            raise ApprovalError(
                f"signal {signal_id} ({sig.status} {sig.kind}) is not retryable")
        asset, base_key, old_arb_id = sig.asset, sig.idempotency_key, sig.arb_id
        avg_apr = sig.trailing_avg_apr

    # 1) Don't churn a healthy/in-flight position.
    cur = _current_arb(asset)
    cur_status = ((cur or {}).get("status") or "").lower()
    if cur_status in ("open", "opening"):
        raise ApprovalError(
            f"{asset} arb is '{cur_status}', not failed — nothing to retry")

    # 2) Free the symbol: close the old/failed arb, then wait for the PM's async
    #    close to clear it (so this stays one click in the common case).
    close_id = (cur or {}).get("arb_id") or old_arb_id
    if close_id is not None:
        try:
            pm.close_arb(arb_id=int(close_id))
        except PMError as exc:
            log.info("retry: close arb %s -> %s (continuing)", close_id, exc)
    for _ in range(6):
        if _current_arb(asset) is None:
            break
        time.sleep(1.0)

    # 3) Re-open with a FRESH idempotency key (old key dedups to the dead arb).
    new_key = _retry_key(base_key)
    try:
        resp = pm.open_arb(
            idempotency_key=new_key, asset=asset,
            size_mode=get_settings().size_mode,
            strategy_tag="hl-cash-and-carry")
    except PMError as exc:
        if "-> 409" in str(exc):
            raise ApprovalError(
                "the previous position is still closing — give it a few "
                "seconds and click Retry again") from exc
        with session_scope() as db:
            s = db.get(Signal, signal_id)
            if s is not None:
                s.status = "error"
                s.error_message = str(exc)[:1000]
        notifier.fire_error(asset, "OPEN", str(exc))
        raise ApprovalError(str(exc)) from exc

    new_arb_id = resp.get("arb_id")
    with session_scope() as db:
        s = db.get(Signal, signal_id)
        s.idempotency_key = new_key
        s.arb_id = int(new_arb_id) if new_arb_id is not None else s.arb_id
        s.status = "fired"
        s.fired_at = datetime.now(timezone.utc)
        s.error_message = ""
        updated = _snapshot(s)

    notifier.opened(asset, updated.arb_id or -1, avg_apr)
    log.info("retried OPEN for %s -> arb_id=%s (key=%s)",
             asset, updated.arb_id, new_key)
    return updated


def _current_arb(asset: str) -> dict | None:
    """The PM's current non-closed arb for ``asset`` (or None). Best-effort."""
    for pos in get_pm_client().positions():
        if (pos.get("asset") or "").upper() == asset.upper() and \
                (pos.get("status") or "").lower() != "closed":
            return pos
    return None


def _retry_key(base_key: str) -> str:
    """Fresh idempotency key from ``base_key``: strip any prior ``-r…`` suffix,
    append a new UTC timestamp — so the PM treats the retry as a new open and our
    UNIQUE column never collides."""
    base = re.sub(r"-r\d+$", "", base_key)
    return f"{base}-r{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"


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
