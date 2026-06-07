"""Thin client for the position-manager's `/funding-arb/*` API.

Contract (from `mochi-position-manager/docs/openapi-funding-arb.yaml`):
  * ``POST /funding-arb/open``  {idempotency_key, asset, size_mode, strategy_tag}
    with header ``X-Arb-Secret`` -> {status, arb_id, idempotency_key, legs}
  * ``POST /funding-arb/close`` {arb_id} -> {status, arb_id}
  * ``GET  /funding-arb/positions`` -> [ArbPositionView, ...]

We send ``size_mode:"min"`` (paper) and OMIT ``legs`` so the PM uses its DEFAULT
single-venue Hyperliquid cash-and-carry combo (long HL spot + short HL perp).

The idempotency_key we send is the SAME deterministic key stored on our
``Signal`` row, so the PM's dedup (UNIQUE on idempotency_key) aligns with ours
— a re-fire of the same signal returns ``status="duplicate"`` rather than
opening twice.

Offline behaviour: when ``TESTING`` or ``DRY_RUN`` is set, ``open``/``close``
return a deterministic stub and NO network call is made. ``positions`` returns
an empty list offline. Tests for the real wire format monkeypatch ``_request``
(or the httpx transport) instead of relying on the stub.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings, get_settings

log = logging.getLogger(__name__)


class PMError(RuntimeError):
    """Raised when the PM funding-arb API returns a non-success response."""


class PMClient:
    def __init__(self, settings: Settings | None = None):
        self._s = settings or get_settings()

    # --- internals ----------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Arb-Secret": self._s.funding_arb_secret}

    def _url(self, path: str) -> str:
        return f"{self._s.pm_base_url.rstrip('/')}{path}"

    def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        """Single HTTP seam to the PM. Monkeypatched in wire-format tests.

        Raises ``PMError`` on any transport error or non-2xx status (the caller
        records the signal as ``error`` and alerts)."""
        url = self._url(path)
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.request(method, url, json=json, headers=self._headers)
        except Exception as exc:  # noqa: BLE001
            raise PMError(f"PM request {method} {path} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise PMError(f"PM {method} {path} -> {resp.status_code}: "
                          f"{resp.text[:300]}")
        if resp.content:
            return resp.json()
        return None

    # --- API ----------------------------------------------------------------

    def open_arb(self, *, idempotency_key: str, asset: str,
                 size_mode: str = "min",
                 strategy_tag: str = "hl-cash-and-carry") -> dict:
        """POST /funding-arb/open with the DEFAULT HL combo (legs omitted).

        Body: ``{idempotency_key, asset, size_mode, strategy_tag}`` — no
        ``notional`` (ignored for size_mode=min), no ``legs`` (default combo).
        Returns the parsed ArbOpenResponse dict {status, arb_id, ...}.
        """
        body = {
            "idempotency_key": idempotency_key,
            "asset": asset,
            "size_mode": size_mode,
            "strategy_tag": strategy_tag,
        }
        if self._s.offline:
            log.info("[offline] would POST /funding-arb/open %s", body)
            # Deterministic stub arb_id from the key so flows are reproducible.
            return {"status": "accepted",
                    "arb_id": abs(hash(idempotency_key)) % 1_000_000,
                    "idempotency_key": idempotency_key, "legs": []}
        return self._request("POST", "/funding-arb/open", json=body)

    def close_arb(self, *, arb_id: int) -> dict:
        """POST /funding-arb/close {arb_id}. Returns ArbCloseResponse dict."""
        body = {"arb_id": arb_id}
        if self._s.offline:
            log.info("[offline] would POST /funding-arb/close %s", body)
            return {"status": "closing", "arb_id": arb_id}
        return self._request("POST", "/funding-arb/close", json=body)

    def positions(self) -> list[dict]:
        """GET /funding-arb/positions -> list of ArbPositionView dicts.

        Display path: never raises. Offline => empty list; a live failure is
        logged and an empty list returned so the dashboard still renders.
        """
        if self._s.offline:
            return []
        try:
            data = self._request("GET", "/funding-arb/positions")
        except PMError as exc:
            log.warning("PM positions fetch failed: %s", exc)
            return []
        return data or []


_client: PMClient | None = None


def get_pm_client() -> PMClient:
    global _client
    if _client is None:
        _client = PMClient()
    return _client
