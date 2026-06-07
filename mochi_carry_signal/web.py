"""FastAPI app: dashboard + approve/reject routes + the lifespan poller.

Mirrors the position-manager's `app/main.py`: lifespan starts a background
hourly poller (sleep-first => network-free startup), shut down cleanly on
exit. The dashboard (`GET /`) shows current funding per coin vs the entry line,
per-coin state, the signal log, and live PM open arbs, with Approve/Reject
buttons on each pending signal. The approve/reject POSTs are form posts gated by
``APP_SECRET``.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .approval import ApprovalError, AuthError, approve, reject
from .config import get_settings
from .data import hyperliquid as hl
from .db import init_db, session_scope
from .models import Signal
from .pm_client import get_pm_client
from .poller import _derive_state, poll_loop
from .signal import Settlement, apr_to_pph, compute_signal, pph_to_apr

log = logging.getLogger(__name__)

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)
    init_db()
    stop_event = asyncio.Event()
    poll_task = asyncio.create_task(poll_loop(stop_event=stop_event))
    log.info("mochi-carry-signal ready. offline=%s assets=%s",
             settings.offline, settings.assets)
    try:
        yield
    finally:
        stop_event.set()
        poll_task.cancel()
        try:
            await poll_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(
    title="mochi-carry-signal",
    version="0.1.0",
    description="Funding-arb SIGNAL GENERATOR — record carry signals, "
                "approve-to-fire to the position-manager funding-arb API.",
    lifespan=lifespan,
)


# --- helpers ----------------------------------------------------------------

def _live_funding_view(settings) -> list[dict]:
    """Per-asset live snapshot: trailing-72h avg APR, current funding APR, spot,
    and derived state. Best-effort — an HL failure yields a blank row, never a
    500 (the dashboard must always render)."""
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    start_ms = now_ms - 24 * 14 * 3_600_000
    entry_apr = settings.entry_apr
    rows: list[dict] = []
    pm_open = _pm_open_assets()
    for asset in settings.assets:
        avg_apr = funding_now_apr = None
        spot_ok = False
        try:
            points = hl.fetch_funding(asset, start_ms, now_ms)
            spot_ok = hl.has_spot(asset)
            settlements = [Settlement(time=p.time,
                                      funding_rate_pph=p.funding_rate_pph)
                           for p in points]
            avg_pph = compute_signal(settlements, now, settings.lookback_hours)
            avg_apr = pph_to_apr(avg_pph)
            if points:
                funding_now_apr = pph_to_apr(points[-1].funding_rate_pph)
        except Exception:  # noqa: BLE001 — display path
            log.exception("live funding view failed for %s", asset)
        with session_scope() as db:
            state = _derive_state(db, pm_open, asset)
        rows.append({
            "asset": asset, "state": state, "spot_available": spot_ok,
            "trailing_avg_apr": avg_apr, "funding_now_apr": funding_now_apr,
            "entry_apr": entry_apr,
            "above_entry": (avg_apr is not None and avg_apr >= entry_apr),
        })
    return rows


def _pm_open_assets() -> set[str]:
    return {(p.get("asset") or "").upper() for p in get_pm_client().positions()
            if (p.get("status") or "").lower() != "closed"} - {""}


def _recent_signals(limit: int = 50) -> list[dict]:
    with session_scope() as db:
        rows = db.execute(
            select(Signal).order_by(Signal.created_at.desc(), Signal.id.desc())
            .limit(limit)
        ).scalars().all()
        return [{
            "id": s.id, "created_at": s.created_at, "asset": s.asset,
            "kind": s.kind, "trailing_avg_apr": s.trailing_avg_apr,
            "funding_now_apr": s.funding_now_apr,
            "spot_available": s.spot_available, "status": s.status,
            "arb_id": s.arb_id, "error_message": s.error_message,
            "idempotency_key": s.idempotency_key,
        } for s in rows]


# --- routes -----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    settings = get_settings()
    funding = _live_funding_view(settings)
    signals = _recent_signals()
    positions = get_pm_client().positions()
    resp = templates.TemplateResponse(request, "dashboard.html", {
        "funding": funding,
        "signals": signals,
        "positions": positions,
        "entry_apr": settings.entry_apr,
        "exit_apr": settings.exit_apr,
        "lookback_hours": settings.lookback_hours,
        "size_mode": settings.size_mode,
        "pm_base_url": settings.pm_base_url,
        "app_secret_required": bool(settings.app_secret),
        "offline": settings.offline,
        "now": datetime.now(timezone.utc),
    })
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.get("/healthz", response_class=HTMLResponse)
def healthz() -> HTMLResponse:
    return HTMLResponse("ok")


def _redirect_home(msg: str = "") -> RedirectResponse:
    # 303 so the POST -> redirect -> GET pattern works in browsers.
    url = "/" + (f"?msg={msg}" if msg else "")
    return RedirectResponse(url=url, status_code=303)


@app.post("/signals/{signal_id}/approve")
def approve_signal(signal_id: int, secret: str = Form(default="")):
    try:
        approve(signal_id, secret=secret)
    except AuthError:
        return HTMLResponse("unauthorized: bad app secret", status_code=401)
    except ApprovalError as exc:
        return HTMLResponse(f"cannot approve: {exc}", status_code=409)
    return _redirect_home("approved")


@app.post("/signals/{signal_id}/reject")
def reject_signal(signal_id: int, secret: str = Form(default="")):
    try:
        reject(signal_id, secret=secret)
    except AuthError:
        return HTMLResponse("unauthorized: bad app secret", status_code=401)
    except ApprovalError as exc:
        return HTMLResponse(f"cannot reject: {exc}", status_code=409)
    return _redirect_home("rejected")
