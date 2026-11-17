"""tpsmydata API adapter.

Chosen for the pilot because registration includes free test credits and
both registers are checked in a single lookup. The exact request and response
shape is not public, so the response parser below is defensive: it accepts a
few common shapes and falls back to ERROR rather than guessing CLEAR.

Before you rely on this in production, run:
    python run.py screen --provider tpsmydata --test 02079460000
and compare the raw payload it prints against your account's API docs. Adjust
_read_flag if the field names differ. Never adjust it in a direction that
turns an unknown response into CLEAR.
"""

from __future__ import annotations

import datetime as dt

import httpx

from app.config import settings
from app.screening.base import (
    SCREEN_CLEAR,
    SCREEN_ERROR,
    SCREEN_REGISTERED,
    ScreenResult,
    ScreeningProvider,
)

_TRUE_WORDS = {"true", "yes", "y", "1", "registered", "listed", "on"}
_FALSE_WORDS = {"false", "no", "n", "0", "clear", "not registered", "notregistered"}


def _read_flag(payload: dict, *keys: str) -> str:
    """Pull one register's verdict out of a response of unknown shape."""
    for key in keys:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool):
            return SCREEN_REGISTERED if value else SCREEN_CLEAR
        text = str(value).strip().lower()
        if text in _TRUE_WORDS:
            return SCREEN_REGISTERED
        if text in _FALSE_WORDS:
            return SCREEN_CLEAR
    return SCREEN_ERROR


class TpsMyDataProvider(ScreeningProvider):
    name = "tpsmydata"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key or settings.tpsmydata_api_key
        if not self.api_key:
            raise RuntimeError(
                "TPSMYDATA_API_KEY is not set. Register at tpsmydata.co.uk, create "
                "an API key, and put it in .env. Until then use "
                "SCREENING_PROVIDER=manual."
            )
        self._client = httpx.Client(
            base_url=base_url or settings.tpsmydata_base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": settings.user_agent,
            },
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def screen_one(self, phone: str) -> tuple[ScreenResult, dict]:
        now = dt.datetime.now(dt.timezone.utc)
        try:
            response = self._client.get("/v1/check", params={"number": phone})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return (
                ScreenResult(
                    phone=phone,
                    tps=SCREEN_ERROR,
                    ctps=SCREEN_ERROR,
                    provider=self.name,
                    checked_at=now,
                    receipt=f"request failed: {type(exc).__name__}: {exc}",
                ),
                {},
            )

        body = payload.get("result", payload) if isinstance(payload, dict) else {}
        tps = _read_flag(body, "tps", "onTps", "tps_registered", "TPS")
        ctps = _read_flag(body, "ctps", "onCtps", "ctps_registered", "CTPS")
        receipt = (
            f"{self.name} checked {phone} at {now.isoformat()} "
            f"ref={body.get('reference') or body.get('id') or 'none'}"
        )
        return (
            ScreenResult(
                phone=phone,
                tps=tps,
                ctps=ctps,
                provider=self.name,
                checked_at=now,
                receipt=receipt,
            ),
            payload if isinstance(payload, dict) else {},
        )

    def screen(self, phones: list[str]) -> dict[str, ScreenResult]:
        return {phone: self.screen_one(phone)[0] for phone in phones}
