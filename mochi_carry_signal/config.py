"""Application config (pydantic-settings), mirroring the position-manager.

`get_settings()` is lru-cached; tests set env BEFORE import and call
`get_settings.cache_clear()` in their conftest (same pattern as the PM).

The two "no network" flags:
  * ``testing`` — set by the test-suite; mocks the PM HTTP call + Telegram.
  * ``dry_run`` — same effect for local/offline dev.
Either one being true means the approve-to-fire path and the notifier perform
no outbound network (they log what they *would* have done).
"""
from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Position-manager (funding-arb execution API) ---
    pm_base_url: str = "http://localhost:8000"
    # Sent as the X-Arb-Secret header; must match the PM's FUNDING_ARB_SECRET.
    funding_arb_secret: str = ""

    # --- This app's auth: gates the approve/reject form POSTs ---
    # Empty => gate is OPEN (local dev). Set it for any shared deploy.
    app_secret: str = ""

    # --- Telegram (this app's OWN bot — SEPARATE from the PM's) ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Signal parameters (LOCKED rule) ---
    # `str | list[str]`, not bare `list[str]`: pydantic-settings' env source
    # JSON-decodes a "complex" (list) field BEFORE field validators run, so a
    # comma-separated ASSETS="BTC,ETH" raised SettingsError. Widening to a Union
    # makes the env source tolerant of a non-JSON value (allow_parse_failure),
    # handing the raw string to `_split_assets` below — which ALWAYS returns a
    # clean list. (2.5.2 lacks the `NoDecode` annotation; this keeps the pin.)
    assets: str | list[str] = Field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    lookback_hours: int = 72
    entry_apr: float = 10.0          # %/yr; OPEN when trailing-avg APR >= this
    exit_apr: float = 0.0            # %/yr; CLOSE when trailing-avg APR <= this
    size_mode: str = "min"           # "min" (paper) or "notional"

    # --- Dashboard ---
    # Rolling window (days) for the per-asset funding-history charts. Display
    # only; the LOCKED signal still uses lookback_hours.
    chart_lookback_days: int = 30

    # --- Poller cadence ---
    poll_seconds: float = 3600.0

    # --- Storage / runtime ---
    database_url: str = "sqlite:///./data/signals.db"
    log_level: str = "INFO"
    dry_run: bool = False
    testing: bool = False

    @field_validator("assets", mode="before")
    @classmethod
    def _split_assets(cls, v):
        """Normalize ASSETS to a clean uppercased list. Accepts either a JSON
        list (``["BTC","ETH"]``) OR a comma-separated string (``"BTC,ETH,SOL"``);
        in both cases: split, strip, uppercase, drop empties. Always returns a
        ``list[str]`` (never leaves a raw string), so the resolved value is a
        list regardless of how it arrived (env, .env, or the default)."""
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                try:
                    v = json.loads(s)          # JSON list -> fall through to list path
                except ValueError:
                    v = s                       # not valid JSON: treat as a 1-item csv
            else:
                return [a.strip().upper() for a in s.split(",") if a.strip()]
        if isinstance(v, (list, tuple)):
            return [str(a).strip().upper() for a in v if str(a).strip()]
        return v

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def offline(self) -> bool:
        """True when no outbound network should happen (tests / dry-run)."""
        return bool(self.testing or self.dry_run)


@lru_cache
def get_settings() -> Settings:
    return Settings()
