# mochi-carry-signal

Funding-arbitrage **SIGNAL GENERATOR** for single-venue Hyperliquid
cash-and-carry (long HL spot + short HL perp, delta-neutral).

It watches live Hyperliquid funding for **BTC / ETH / SOL**, computes a trailing
cash-and-carry carry signal, **records** every signal, shows a dashboard, and —
**only on your explicit approval** — fires an order at the
[position-manager](../mochi-position-manager)'s funding-arb API. It is a sibling
to [mochi-carry-backtester](../mochi-carry-backtester): the backtester *tunes*
the rule offline; this service *runs* it live.

> **It never fires automatically. Approve-to-fire only.** The poller only ever
> *records* a pending signal; an order is sent to the position-manager solely
> when you click **Approve** in the dashboard.

## The LOCKED signal rule

Identical to the backtester (`mochi_carry_backtester/strategy.py::compute_signal`
+ `display.py::apr_to_pph`), evaluated at the current instant:

- Signal = trailing average of **`funding_rate_pph`** (= `funding_rate_native /
  interval_hours`, the fractional per-hour rate, **no ×100**) over the last
  **72h** of *settlements*.
- The window is **right-closed** `(now − 72h, now]` with **no look-ahead**: a
  settlement at exactly `now` counts; one strictly after `now` (or exactly at
  `now − 72h`) never does, so a future settlement can never change an earlier
  decision.
- **OPEN** when the trailing average ≥ `apr_to_pph(10)` (10 %/yr) **AND** a
  tradable HL spot market exists for the coin (the cash-and-carry spot gate).
- **CLOSE** when the trailing average ≤ 0.
- When the window is empty (signal undefined) the state is **held** — never
  traded on.

Funding/thresholds are stored as fractional per-hour internally; the dashboard
renders them as **annualized %** (`× 24 × 365 × 100`) for readability.

## Architecture

FastAPI service mirroring the position-manager's conventions (lifespan
background poller, SQLite via SQLAlchemy, pydantic-settings, a dark-theme HTML
dashboard, its own best-effort Telegram notifier).

```
mochi_carry_signal/
  config.py            pydantic-settings; get_settings() lru-cached; .offline = TESTING|DRY_RUN
  data/hyperliquid.py  MINIMAL vendored HL seam: _post (HTTP) -> fetch_funding (pph normalized)
                       + has_spot(asset) via spotMeta. _post is monkeypatched offline in tests.
  signal.py            PURE: compute_signal(settlements, now, lookback_h=72) (right-closed, no
                       look-ahead) + decide(state, avg_pph, spot_ok, entry_pph, exit_pph)
  chart.py             PURE: build_funding_chart(...) -> inline-SVG coordinates for the dashboard's
                       1-month funding history (raw + trailing line + entry/exit markers). No IO.
  models.py            Signal{... idempotency_key UNIQUE, status, arb_id, ...}
  db.py                SQLite engine + session_scope (WAL, busy_timeout)
  poller.py            poll_once()/poll_loop(): fetch -> compute -> derive state -> on a CHANGE
                       insert ONE pending Signal + Telegram alert (idempotent via idempotency_key)
  approval.py          approve()/reject(): the approve-to-fire core (gated by APP_SECRET)
  pm_client.py         thin client for POST /funding-arb/{open,close} + GET /positions (X-Arb-Secret)
  notifier.py          this app's OWN Telegram bot (SEPARATE from the PM's), best-effort
  web.py               FastAPI app: GET / dashboard, POST /signals/{id}/{approve,reject}, lifespan
  templates/dashboard.html
  run.py               uvicorn entrypoint
```

### Flow: poll → record → approve → fire

1. **Poll** (hourly, in the lifespan; sleeps first so startup is network-free).
   For each asset: fetch recent HL funding + spot availability, compute the
   trailing-72h signal + current funding, derive the asset's current state from
   the latest non-rejected `Signal` **and** live PM open arbs.
2. **Record.** On a state **change**, insert ONE `pending` `Signal` and send a
   Telegram alert. A repeat poll in the same state inserts nothing — dedup is a
   deterministic `idempotency_key` per `(asset, transition, hour)` + a UNIQUE
   constraint.
3. **Approve.** You review pending signals on the dashboard and click
   **Approve** (gated by `APP_SECRET`) — or **Reject**.
4. **Fire.** On approve:
   - pending **OPEN** → `POST {PM}/funding-arb/open` with header
     `X-Arb-Secret`, body `{idempotency_key (the signal's), asset,
     size_mode:"min", strategy_tag:"hl-cash-and-carry"}` — **no `legs`** (the PM
     uses its default single-venue HL combo) and **no `notional`** (ignored for
     `size_mode:"min"`). The returned `arb_id` is stored, the signal marked
     `fired`, and a Telegram alert sent.
   - pending **CLOSE** → `POST {PM}/funding-arb/close` `{arb_id}` for the
     matching open arb; signal marked `fired`, alert sent.

   Because the signal's `idempotency_key` is the **same** key sent to the PM,
   the PM's dedup aligns with ours — re-firing the same signal returns
   `status="duplicate"` instead of opening twice.

## Configuration

`pydantic-settings`, read from `.env` (see `.env.example`). Key vars:

| var | default | meaning |
|---|---|---|
| `PM_BASE_URL` | `http://localhost:8000` | position-manager base URL |
| `FUNDING_ARB_SECRET` | _(empty)_ | sent as `X-Arb-Secret`; **must equal the PM's `FUNDING_ARB_SECRET`** |
| `APP_SECRET` | _(empty)_ | gates approve/reject; empty ⇒ gate OPEN (dev only) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | _(empty)_ | **this app's own** bot/chat (separate from the PM's) |
| `ASSETS` | `["BTC","ETH","SOL"]` | coins to watch |
| `LOOKBACK_HOURS` | `72` | trailing-average window |
| `ENTRY_APR` / `EXIT_APR` | `10.0` / `0.0` | %/yr thresholds |
| `SIZE_MODE` | `min` | sent to the PM (`min` = paper-sized, tiny real orders) |
| `POLL_SECONDS` | `3600` | poll cadence |
| `DATABASE_URL` | `sqlite:///./data/signals.db` | SQLite store |
| `DRY_RUN` / `TESTING` | `false` | mock the PM HTTP call **and** Telegram (no network) |

### Separate Telegram bot

This service has its **own** Telegram notifier (`notifier.py`) using
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — deliberately **distinct** from the
position-manager's bot/chat, so signal alerts and execution alerts don't
collide. Create a separate bot via @BotFather and use a separate chat id. Like
the PM, sends are best-effort: a failure is logged, never raised, and never
blocks the poll/approve path. In `TESTING`/`DRY_RUN` it sends nothing.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # fill in FUNDING_ARB_SECRET (match the PM), APP_SECRET, Telegram
python run.py                 # http://127.0.0.1:8100   (env: HOST, PORT, RELOAD)
# equivalently:
uvicorn mochi_carry_signal.web:app --host 127.0.0.1 --port 8100
# or the console script:
mochi-carry-signal
```

Local dry-run with no live trading (mocks PM + Telegram, still records signals):

```bash
DRY_RUN=true python run.py
```

**Live end-to-end** needs the position-manager running with its funding-arb API
configured (`FUNDING_ARB_SECRET` set there) and the **same** secret set here.

## Tests

Fully offline — temp SQLite DB, `TESTING=true`, no network or Telegram (the HL
`_post` seam and the PM HTTP call are monkeypatched / mocked).

```bash
pip install -e ".[dev]"
python -m pytest
python -m pytest tests/test_signal.py -v
```

Coverage: the signal rule (trailing-72h pph, right-closed / no-look-ahead, the
thresholds, spot gate), the state machine, the poller (one pending signal per
transition + idempotent repeat), approve-to-fire (the exact `/funding-arb/open`
wire request incl. `X-Arb-Secret` + `size_mode:"min"` + idempotency_key; close;
reject; auth), the dashboard (200 with funding + signals + mocked positions),
the notifier (message format + best-effort), and config/data normalization.

### Contract conformance with the position-manager

The funding-arb HTTP contract is **owned by** `mochi-position-manager`; this app
conforms to it. A pinned copy of the provider's OpenAPI spec is vendored at
`tests/contract/openapi-funding-arb.yaml`, and `tests/test_pm_contract.py`
validates the real outgoing `open`/`close` requests against it — so contract
drift surfaces as a **failing test here**, automatically.

When the provider changes the contract (edit its schemas, `make openapi` there),
re-sync the pinned copy and re-run the contract test:

```bash
make vendor-contract       # cp ../mochi-position-manager/docs/openapi-funding-arb.yaml -> tests/contract/
# (override the provider location: make vendor-contract PM_REPO=/path/to/mochi-position-manager)
python -m pytest tests/test_pm_contract.py
```
