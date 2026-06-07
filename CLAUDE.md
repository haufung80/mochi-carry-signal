# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The funding-arb **SIGNAL GENERATOR** (BTC/ETH/SOL). It watches live Hyperliquid funding, computes the
trailing cash-and-carry signal, **RECORDS** every signal, dashboards them, and — **only on the USER's
explicit approval** — fires the order to `mochi-position-manager`'s funding-arb API. It **never fires
automatically: approve-to-fire only**. FastAPI service on port **8100** (the position-manager is 8000).
It decides and records; it does NOT execute — the position-manager owns both legs, retries, and PnL.

## Commands

```bash
pip install -e ".[dev]"                # repo also on sys.path via tests/conftest.py
python run.py                          # http://127.0.0.1:8100  (env: HOST, PORT, RELOAD)
uvicorn mochi_carry_signal.web:app --port 8100     # equivalently
mochi-carry-signal                     # console script (-> mochi_carry_signal.run:main)

# Tests — fully offline (HL `_post`, the PM HTTP call, and Telegram are all mocked).
python -m pytest
python -m pytest tests/test_signal.py -v
```

No linter/formatter configured. `pyproject.toml` packaging with **pinned** versions (mirrors the PM's
pinning discipline). `[tool.pytest.ini_options]` sets `asyncio_mode=auto`.

## The LOCKED signal rule (don't drift it)

`mochi_carry_signal/signal.py` is a **deliberate live PORT** of the backtester's
`strategy.py::compute_signal` (+ `display.py::apr_to_pph`), reduced to a point-in-time evaluation:

- Signal = trailing average of **`funding_rate_pph`** (= `funding_rate_native / interval_hours`,
  fractional per-hour, **no ×100**) over the last **72h** of *settlements*.
- Window is **right-closed `(now − L, now]`, NO look-ahead**: a settlement at exactly `now` counts; one
  strictly after `now` or exactly at `now − L` never does. Empty window → `None` → **HOLD** (never trade
  on `None`).
- **OPEN** (FLAT→OPEN) when `avg ≥ apr_to_pph(10)` (10 %/yr) **AND** an HL spot market exists for the
  coin (the cash-and-carry spot gate). **CLOSE** (OPEN→CLOSE) when `avg ≤ 0`.
- **Units are load-bearing:** thresholds are compared DIRECTLY to the fractional per-hour rate (the
  engine unit). `signal.py` is pure (`compute_signal` + `decide`) — fully unit-tested, no IO.

## Architecture (poll → record → approve → fire)

```
signal.py            PURE: compute_signal(settlements, now, lookback_h=72) + decide(state, avg, spot, ...)
chart.py             PURE: build_funding_chart(...) -> inline-SVG coords for the dashboard's rolling
                     1-month funding history (raw + trailing line + entry/exit markers). Trailing line
                     reuses the LOCKED compute_signal at each point; no IO, fully unit-tested.
data/hyperliquid.py  MINIMAL VENDORED HL seam (not a dependency on the backtester): `_post` is the one
                     monkeypatched HTTP seam; fetch_funding -> funding_rate_pph = native/interval (per-
                     settlement interval inferred from deltas); has_spot(asset) via spotMeta
poller.py            poll_once()/poll_loop(): hourly, SLEEP-FIRST (network-free startup). Derives each
                     asset's resting state from (live PM open arbs) ∪ (our latest non-rejected Signal);
                     on a state CHANGE inserts ONE pending Signal + Telegram. NEVER fires an order.
models.py            Signal{status: pending|approved|fired|rejected|error; idempotency_key UNIQUE; arb_id}
approval.py          approve()/reject(): the approve-to-fire core, gated by APP_SECRET
pm_client.py         thin PM client (X-Arb-Secret); OFFLINE stub when DRY_RUN/TESTING (no network)
web.py               FastAPI: GET / dashboard (funding-history charts + signal log + PM arbs), POST
                     /signals/{id}/{approve,reject}, /healthz, lifespan poller
notifier.py          this app's OWN Telegram bot — SEPARATE from the PM's; best-effort, never raises
config.py / db.py / run.py    settings (pydantic-settings) / SQLite+session_scope / uvicorn entrypoint
```

- **Idempotency** is a deterministic `idempotency_key` per `(asset, transition, HOUR)`
  (`sig-<ISO-hour>Z-<ASSET>-<KIND>`) + the UNIQUE column — a repeat poll in the same state/hour inserts
  nothing. This SAME key is sent to the PM `/open`, so the PM's dedup aligns with ours.
- **State derivation** combines the PM (truth for OPEN) and our signal log so we stay correct when the
  PM is briefly unreachable or a CLOSE hasn't fired yet; an errored OPEN still HOLDS the OPEN state (so
  we don't re-fire on top of a half-open arb).

## Contract to the position-manager

The seam is the PM's OpenAPI funding-arb contract (`mochi-position-manager/docs/openapi-funding-arb.yaml`).
On approve:

- **OPEN** → `POST {PM_BASE_URL}/funding-arb/open {idempotency_key, asset, size_mode:"min",
  strategy_tag:"hl-cash-and-carry"}` with header `X-Arb-Secret` — **`legs` omitted** (PM uses its
  DEFAULT single-venue HL combo: long HL spot + short HL perp) and **no `notional`** (ignored for
  `size_mode:"min"`). Store the returned `arb_id`, mark `fired`.
- **CLOSE** → `POST /funding-arb/close {arb_id}` for the matching open arb.

`FUNDING_ARB_SECRET` here **must equal the PM's `FUNDING_ARB_SECRET`**. The signal's `idempotency_key`
IS the PM dedup key.

### Contract change protocol (the PM owns it; we conform)

The funding-arb HTTP contract is the **seam** with `mochi-position-manager`, and the **PM OWNS it**; this
app is a consumer that **conforms**. A PINNED copy of the provider's spec is **vendored** at
`tests/contract/openapi-funding-arb.yaml`, and `tests/test_pm_contract.py` validates our real outgoing
`open`/`close` requests against it — so **contract drift fails as a test HERE**, not as a human's job to
remember. To change the contract:

1. Change it **in the provider**: edit `mochi-position-manager`'s funding-arb schemas, then `make openapi`
   there to regenerate `docs/openapi-funding-arb.yaml`.
2. **Re-vendor** it here: `make vendor-contract` (copies the provider spec → `tests/contract/`; override
   the provider location with `PM_REPO=/path/to/mochi-position-manager`).
3. **Update `pm_client.py`** to match, and run `python -m pytest tests/test_pm_contract.py`.

If `pm_client` drifts from the vendored spec (or a re-vendor is corrupt/unexpected), that test fails.

## Configuration (`config.py`, pydantic-settings, `.env` / `.env.example`)

`get_settings()` is lru-cached; tests set env BEFORE import and `cache_clear()`. Key vars / defaults:
`PM_BASE_URL` (`http://localhost:8000`), `FUNDING_ARB_SECRET` (sent as `X-Arb-Secret`), `APP_SECRET`
(gates approve/reject; empty ⇒ gate OPEN, dev only), `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`,
`ASSETS` (`["BTC","ETH","SOL"]` — accepts a comma-string OR a JSON list), `LOOKBACK_HOURS` (`72`),
`ENTRY_APR` (`10`), `EXIT_APR` (`0`), `SIZE_MODE` (`min`), `CHART_LOOKBACK_DAYS` (`180` ≈ 6 months,
dashboard display only), `CHART_CACHE_SECONDS` (`600`; dashboard funding-fetch cache TTL, poller never
cached), `POLL_SECONDS` (`3600`), `DATABASE_URL`,
`DRY_RUN`/`TESTING`. **`offline = testing or dry_run`** ⇒ the PM call AND Telegram do no outbound
network (they log what they'd have done); the dashboard still renders.

## Conventions / decisions

- The signal logic is a **deliberate PORT** of `mochi-carry-backtester` — the HL data seam is **VENDORED**
  (a thin `data/hyperliquid.py`), NOT a dependency on the backtester package. Keep the funding/interval
  conventions identical so the live signal matches the backtest bit-for-bit.
- This app has its **own** Telegram bot/chat, distinct from the PM's, so signal vs execution alerts don't
  collide.
- The dashboard renders funding/thresholds as **annualized %** (`×24×365×100`) for readability; the
  engine/config stay in fractional per-hour.
- The dashboard's funding-history chart is **server-rendered inline SVG** (`chart.py` computes coords,
  the template draws them) — **no JS, no CDN**, so it renders in offline/dry-run mode. Its trailing line
  reuses the LOCKED `compute_signal` (no second implementation to drift); raw spikes are clamped onto the
  scale (robust 2–98 pct domain) so they can't squash the signal line. The HL fetch window widens to
  `CHART_LOOKBACK_DAYS×24 + LOOKBACK_HOURS` so the trailing line is valid from the first displayed day.
  For long (6-month+) windows the trailing avg is computed in O(N) (bisected window slice → still the
  LOCKED rule, just not rescanning every settlement per point) and the drawn polyline is strided to
  `_MAX_DRAW_POINTS` (the avg still uses every settlement) so a multi-month page stays fast and lean.
- The dashboard caches the HL funding fetch in-memory per asset (`web._funding_cache`, TTL
  `CHART_CACHE_SECONDS`) so rapid refreshes don't re-paginate months of history each load; the POLLER
  never reads this cache (it always fetches fresh). Tests clear the cache in the autouse conftest fixture.

## This is a 3-app system

`mochi-carry-backtester` (research / tuning the carry rule) → **`mochi-carry-signal`** (this repo: live
decision + approve-to-fire) → `mochi-position-manager` (delta-neutral execution + reporting). The
integration seam is the **OpenAPI funding-arb contract**
(`mochi-position-manager/docs/openapi-funding-arb.yaml`); the signal rule is shared by porting from the
backtester into this repo's `signal.py`.
