"""Config resolution + DRY_RUN/offline gating + the HL data normalization seam."""
import os
from datetime import datetime, timezone

from mochi_carry_signal.config import Settings, get_settings


def test_config_resolves_with_test_env():
    s = get_settings()
    assert s.assets == ["BTC", "ETH", "SOL"]
    assert s.lookback_hours == 72
    assert s.entry_apr == 10.0
    assert s.exit_apr == 0.0
    assert s.size_mode == "min"
    assert s.funding_arb_secret == "test-arb-secret"
    assert s.app_secret == "test-app-secret"


def test_offline_true_under_testing():
    # conftest sets TESTING=true.
    assert get_settings().offline is True


def test_dry_run_implies_offline():
    s = Settings(testing=False, dry_run=True, funding_arb_secret="x")
    assert s.offline is True


def test_assets_accepts_csv_string():
    s = Settings(assets="BTC, ETH", funding_arb_secret="x")
    assert s.assets == ["BTC", "ETH"]


def test_assets_accepts_json_list_env(monkeypatch):
    monkeypatch.setenv("ASSETS", '["BTC","SOL"]')
    get_settings.cache_clear()
    try:
        assert get_settings().assets == ["BTC", "SOL"]
    finally:
        monkeypatch.delenv("ASSETS", raising=False)
        get_settings.cache_clear()


# --- HL data seam (monkeypatched _post; no network) -------------------------

def test_fetch_funding_normalizes_pph(monkeypatch):
    """fetch_funding turns raw HL rows into FundingPoints with pph = native/interval."""
    from mochi_carry_signal.data import hyperliquid as hl

    base_ms = int(datetime(2026, 6, 7, 0, tzinfo=timezone.utc).timestamp() * 1000)
    hour = 3_600_000
    rows = [
        {"time": base_ms, "fundingRate": "0.00001"},
        {"time": base_ms + hour, "fundingRate": "0.00002"},
        {"time": base_ms + 2 * hour, "fundingRate": "0.00003"},
    ]
    monkeypatch.setattr(hl, "_post", lambda body: rows)

    pts = hl.fetch_funding("BTC", base_ms, base_ms + 3 * hour)
    assert len(pts) == 3
    # Hourly settlements => interval ≈ 1h => pph == native.
    assert pts[1].interval_hours == 1.0
    assert pts[1].funding_rate_pph == 0.00002
    # ascending by time
    assert pts[0].time < pts[1].time < pts[2].time


def test_fetch_funding_infers_interval_from_gaps(monkeypatch):
    """An 8h gap => interval 8h => pph = native / 8 (the backtester convention)."""
    from mochi_carry_signal.data import hyperliquid as hl

    base_ms = int(datetime(2026, 6, 7, 0, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [
        {"time": base_ms, "fundingRate": "0.0008"},
        {"time": base_ms + 8 * 3_600_000, "fundingRate": "0.0008"},
    ]
    monkeypatch.setattr(hl, "_post", lambda body: rows)
    pts = hl.fetch_funding("BTC", base_ms, base_ms + 8 * 3_600_000)
    assert pts[1].interval_hours == 8.0
    assert pts[1].funding_rate_pph == 0.0008 / 8.0


def test_has_spot_resolves_via_spotmeta(monkeypatch):
    from mochi_carry_signal.data import hyperliquid as hl

    meta = {
        "tokens": [{"index": 0, "name": "USDC"}, {"index": 1, "name": "UBTC"}],
        "universe": [{"name": "UBTC/USDC", "tokens": [1, 0]}],
    }
    monkeypatch.setattr(hl, "_post", lambda body: meta)
    assert hl.has_spot("BTC") is True
    assert hl.has_spot("ETH") is False


def test_has_spot_never_raises(monkeypatch):
    from mochi_carry_signal.data import hyperliquid as hl

    def boom(body):
        raise RuntimeError("network down")

    monkeypatch.setattr(hl, "_post", boom)
    assert hl.has_spot("BTC") is False


def test_pm_client_offline_open_no_network():
    """PMClient.open_arb in offline mode returns a stub and never hits network."""
    from mochi_carry_signal.pm_client import PMClient

    c = PMClient(get_settings())
    resp = c.open_arb(idempotency_key="k-abc", asset="BTC")
    assert resp["status"] == "accepted"
    assert isinstance(resp["arb_id"], int)
    assert c.positions() == []     # offline => empty
