"""Test config — FULLY OFFLINE. Mirrors the position-manager's conftest.

Sets env BEFORE importing app modules (pydantic-settings reads at import), uses
a temp SQLite DB, forces TESTING=true (mocks the PM HTTP call + Telegram), and
puts the repo on sys.path so tests run without an install.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Repo root on sys.path (run without `pip install -e .`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Env BEFORE any app import. TESTING => no outbound network (PM + Telegram).
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("APP_SECRET", "test-app-secret")
os.environ.setdefault("FUNDING_ARB_SECRET", "test-arb-secret")
os.environ.setdefault("PM_BASE_URL", "http://pm.test")

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"

from mochi_carry_signal.config import get_settings  # noqa: E402

get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_db():
    """Fresh schema per test."""
    from mochi_carry_signal.db import Base, engine, init_db
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset cached notifier / PM client + the dashboard funding cache so
    per-test monkeypatches and fixtures aren't seen by a later test."""
    from mochi_carry_signal import notifier as notif_mod
    from mochi_carry_signal import pm_client as pm_mod
    from mochi_carry_signal import web as web_mod
    notif_mod._notifier = None
    pm_mod._client = None
    web_mod._funding_cache.clear()
    yield
    notif_mod._notifier = None
    pm_mod._client = None
    web_mod._funding_cache.clear()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def fake_hl(monkeypatch):
    """Monkeypatch the HL data seam so the poller/dashboard run offline.

    Returns a mutable controller: set ``.funding[asset] = [FundingPoint...]`` and
    ``.spot[asset] = True/False``. ``fetch_funding`` windows by [start_ms,end_ms].
    """
    from mochi_carry_signal.data import hyperliquid as hl

    class _Fake:
        def __init__(self):
            self.funding: dict[str, list] = {}
            self.spot: dict[str, bool] = {}
            self.fail_assets: set[str] = set()

        def fetch_funding(self, asset, start_ms, end_ms):
            if asset.upper() in self.fail_assets:
                raise RuntimeError("boom")
            pts = self.funding.get(asset.upper(), [])
            lo = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
            hi = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
            return [p for p in pts if lo <= p.time <= hi]

        def has_spot(self, asset):
            return self.spot.get(asset.upper(), True)

    fake = _Fake()
    monkeypatch.setattr(hl, "fetch_funding", fake.fetch_funding)
    monkeypatch.setattr(hl, "has_spot", fake.has_spot)
    return fake


@pytest.fixture
def spy_notifier(monkeypatch):
    """Record notifier calls without sending (offline already, but assertable)."""
    from mochi_carry_signal import notifier as notif_mod

    class _Spy:
        enabled = False

        def __init__(self):
            self.calls: list[tuple] = []

        def __getattr__(self, name):
            def _rec(*a, **kw):
                self.calls.append((name, a, kw))
            return _rec

    spy = _Spy()
    monkeypatch.setattr(notif_mod, "_notifier", spy)
    return spy


def make_points(asset_rates, *, base: datetime, step_h: int = 1):
    """Build a list of FundingPoint from per-hour native rates.

    ``asset_rates`` is a list of native fractional rates; settlements are placed
    every ``step_h`` hours ending at ``base`` (so the last rate settles at
    ``base``). interval_hours = step_h, pph = native / step_h.
    """
    from datetime import timedelta

    from mochi_carry_signal.data.hyperliquid import FundingPoint

    n = len(asset_rates)
    pts = []
    for i, native in enumerate(asset_rates):
        t = base - timedelta(hours=step_h * (n - 1 - i))
        pts.append(FundingPoint(
            time=t, funding_rate_native=native, interval_hours=step_h,
            funding_rate_pph=native / step_h))
    return pts
