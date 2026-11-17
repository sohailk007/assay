"""Screening provider registry."""

from __future__ import annotations

from app.config import settings
from app.screening.base import ScreeningProvider, ScreenResult

__all__ = ["ScreeningProvider", "ScreenResult", "get_provider"]


def get_provider(name: str | None = None, **kwargs) -> ScreeningProvider:
    chosen = (name or settings.screening_provider or "manual").lower()
    if chosen in {"manual", "manual-csv", "csv"}:
        from app.screening.manual import ManualCsvProvider

        return ManualCsvProvider(
            result_path=kwargs.get("result_path", "screening/screened.csv"),
            receipt=kwargs.get("receipt"),
        )
    if chosen == "tpsmydata":
        from app.screening.tpsmydata import TpsMyDataProvider

        return TpsMyDataProvider()
    raise ValueError(
        f"Unknown screening provider {chosen!r}. Valid: manual, tpsmydata."
    )
