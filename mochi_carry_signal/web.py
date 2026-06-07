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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .approval import ApprovalError, AuthError, approve, reject
from .chart import SignalEvent, build_funding_chart
from .config import get_settings
from .data import hyperliquid as hl
from .db import init_db, session_scope
from .models import Signal
from .pm_client import get_pm_client
from .poller import _derive_state, poll_loop
from .signal import Settlement, compute_signal, pph_to_apr

# Inline-SVG funding chart canvas (viewBox units; scaled to container by CSS).
_CHART_W, _CHART_H = 720, 240

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

def _as_utc(dt: datetime) -> datetime:
    """Normalize a (possibly naive — SQLite drops tz) timestamp to tz-aware UTC.

    We always STORE UTC, so a naive value read back is interpreted as UTC. This
    keeps chart-time comparisons (against tz-aware ``now``) from raising."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _signals_by_asset(since: datetime, limit: int = 500) -> dict[str, list]:
    """Recent recorded signals grouped by asset, for marking on the charts.

    Pulls the most recent ``limit`` rows and keeps those at/after ``since`` (the
    chart window). Filtering in Python (after normalizing to UTC) avoids
    SQLite's naive-datetime comparison quirks."""
    out: dict[str, list] = {}
    with session_scope() as db:
        rows = db.execute(
            select(Signal).order_by(Signal.created_at.desc(), Signal.id.desc())
            .limit(limit)
        ).scalars().all()
        for s in rows:
            t = _as_utc(s.created_at)
            if t < since:
                continue
            out.setdefault(s.asset.upper(), []).append(SignalEvent(
                time=t, kind=s.kind,
                trailing_avg_apr=s.trailing_avg_apr, status=s.status))
    return out


def _live_funding_view(settings) -> list[dict]:
    """Per-asset snapshot (trailing avg / current funding / spot / state) PLUS a
    one-month funding ``chart`` (raw + trailing line + entry/exit markers).

    One HL fetch per asset (chart window + lookback so the trailing line is valid
    from day one) feeds both the snapshot and the chart. Best-effort — an HL
    failure yields a blank row with ``chart=None``, never a 500 (the dashboard
    must always render)."""
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    chart_days = settings.chart_lookback_days
    lookback_h = settings.lookback_hours
    # Fetch the display window PLUS the lookback so trailing is correct from day 0.
    fetch_hours = chart_days * 24 + lookback_h
    start_ms = now_ms - fetch_hours * 3_600_000
    entry_apr = settings.entry_apr
    exit_apr = settings.exit_apr
    sig_by_asset = _signals_by_asset(now - timedelta(days=chart_days))
    rows: list[dict] = []
    pm_open = _pm_open_assets()
    for asset in settings.assets:
        avg_apr = funding_now_apr = None
        spot_ok = False
        chart = None
        try:
            points = hl.fetch_funding(asset, start_ms, now_ms)
            spot_ok = hl.has_spot(asset)
            settlements = [Settlement(time=p.time,
                                      funding_rate_pph=p.funding_rate_pph)
                           for p in points]
            avg_apr = pph_to_apr(compute_signal(settlements, now, lookback_h))
            if points:
                funding_now_apr = pph_to_apr(points[-1].funding_rate_pph)
            chart = build_funding_chart(
                asset, settlements, sig_by_asset.get(asset.upper(), []), now,
                lookback_h=lookback_h, chart_days=chart_days,
                entry_apr=entry_apr, exit_apr=exit_apr,
                width=_CHART_W, height=_CHART_H)
        except Exception:  # noqa: BLE001 — display path
            log.exception("asset view failed for %s", asset)
        with session_scope() as db:
            state = _derive_state(db, pm_open, asset)
        rows.append({
            "asset": asset, "state": state, "spot_available": spot_ok,
            "trailing_avg_apr": avg_apr, "funding_now_apr": funding_now_apr,
            "entry_apr": entry_apr,
            "above_entry": (avg_apr is not None and avg_apr >= entry_apr),
            "chart": chart,
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
        "chart_days": settings.chart_lookback_days,
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
