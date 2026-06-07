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
    assets: list[str] = Field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    lookback_hours: int = 72
    entry_apr: float = 10.0          # %/yr; OPEN when trailing-avg APR >= this
    exit_apr: float = 0.0            # %/yr; CLOSE when trailing-avg APR <= this
    size_mode: str = "min"           # "min" (paper) or "notional"

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
        """Accept a comma-separated string ("BTC,ETH") as well as a JSON list."""
        if isinstance(v, str):
            s = v.strip()
            if s and not s.startswith("["):
                return [a.strip().upper() for a in s.split(",") if a.strip()]
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
