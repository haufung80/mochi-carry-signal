"""Telegram notifier — this app's OWN bot, SEPARATE from the position-manager's.

Same fire-and-forget contract as the PM's notifier (`app/notifier.py`):
failures are logged, NEVER raised — a flaky notification channel must not block
the poll/approve path. Uses ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` from
THIS app's settings, which are distinct from the PM's bot/chat.

In offline mode (``TESTING`` or ``DRY_RUN``) the notifier never sends — it logs
what it would have sent — so the test-suite and local dev make no network
calls.
"""
from __future__ import annotations

import logging

import httpx

from .config import get_settings

log = logging.getLogger(__name__)


def _fmt_apr(apr: float | None) -> str:
    return "n/a" if apr is None else f"{apr:+.2f}%/yr"


class TelegramNotifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        s = get_settings()
        self._token = token or s.telegram_bot_token
        self._chat_id = chat_id or s.telegram_chat_id
        self._offline = s.offline

    @property
    def enabled(self) -> bool:
        # Disabled when creds are missing OR when offline (tests / dry-run).
        return bool(self._token and self._chat_id) and not self._offline

    def send(self, text: str, *, urgent: bool = False) -> None:
        if not self.enabled:
            log.info("Telegram disabled, would have sent: %s", text)
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_notification": not urgent,
        }
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(url, json=payload)
            if resp.status_code != 200:
                log.warning("Telegram send failed: %s %s",
                            resp.status_code, resp.text[:200])
        except Exception as e:  # noqa: BLE001 — best-effort; never raise
            log.warning("Telegram send exception: %s", e)

    # ---- formatted helpers ----

    def signal_generated(self, asset: str, kind: str, avg_apr: float,
                         funding_now_apr: float | None) -> None:
        emoji = "🟢" if kind == "OPEN" else "🔴"
        self.send(
            f"{emoji} *Carry signal: {kind}* `{asset}`\n"
            f"• Trailing-72h avg: *{_fmt_apr(avg_apr)}*\n"
            f"• Funding now: {_fmt_apr(funding_now_apr)}\n"
            "• Status: *pending approval* — approve in the dashboard to fire.",
            urgent=True,
        )

    def opened(self, asset: str, arb_id: int, avg_apr: float) -> None:
        self.send(
            f"✅ *OPEN fired* `{asset}`\n"
            f"• arb_id: `{arb_id}`\n"
            f"• Trailing-72h avg at signal: {_fmt_apr(avg_apr)}\n"
            "• Long HL spot / short HL perp (single-venue cash-and-carry).",
        )

    def closed(self, asset: str, arb_id: int) -> None:
        self.send(
            f"☑️ *CLOSE fired* `{asset}`\n"
            f"• Closed arb_id: `{arb_id}`",
        )

    def rejected(self, asset: str, kind: str) -> None:
        self.send(
            f"🚫 *Signal rejected* `{asset}` {kind}\n"
            "• No order sent.",
        )

    def fire_error(self, asset: str, kind: str, error: str) -> None:
        self.send(
            f"🚨 *Fire FAILED* `{asset}` {kind}\n"
            f"• Error: `{error[:300]}`\n"
            "• Signal left in error state — investigate and retry.",
            urgent=True,
        )


_notifier: TelegramNotifier | None = None


def get_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
