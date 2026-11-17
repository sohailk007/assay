"""Manual screening via CSV, for the pilot and for any provider without an API.

Workflow:
  1. python run.py screen --export screening/to_screen.csv
  2. Upload that file to your screening provider's bulk checker.
  3. Save their result file as screening/screened.csv with columns
     phone,tps,ctps  (values CLEAR or REGISTERED, case insensitive).
  4. python run.py screen --import screening/screened.csv --receipt "tpsmydata bulk 2026-11-16 ref 12345"

The receipt string is what you show if anyone ever asks who screened the
number and when. Put the provider, the date and their reference in it.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from app.screening.base import (
    SCREEN_CLEAR,
    SCREEN_ERROR,
    SCREEN_REGISTERED,
    ScreenResult,
    ScreeningProvider,
)


def _normalise(value: str) -> str:
    v = (value or "").strip().upper()
    if v in {"CLEAR", "NOT REGISTERED", "NOT_REGISTERED", "N", "NO", "FALSE", "0"}:
        return SCREEN_CLEAR
    if v in {"REGISTERED", "ON TPS", "ON_TPS", "Y", "YES", "TRUE", "1"}:
        return SCREEN_REGISTERED
    return SCREEN_ERROR


class ManualCsvProvider(ScreeningProvider):
    name = "manual-csv"

    def __init__(self, result_path: str | Path, receipt: str | None = None) -> None:
        self.result_path = Path(result_path)
        self.receipt = receipt or f"manual CSV import from {self.result_path.name}"
        self._rows: dict[str, tuple[str, str]] = {}

        # Refuse a missing file rather than quietly marking every number
        # ERROR. Both outcomes are safe, because ERROR never opens a gate,
        # but only one of them tells the person what actually went wrong.
        if not self.result_path.exists():
            raise FileNotFoundError(self.result_path)
        self._load()

        if not self._rows:
            raise RuntimeError(
                f"{self.result_path} has no usable rows. It needs a header line "
                "reading exactly: phone,tps,ctps
"
                "  tps and ctps must each say CLEAR or REGISTERED."
            )

    def _load(self) -> None:
        with self.result_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
            missing = {"phone", "tps", "ctps"} - headers
            if missing:
                raise RuntimeError(
                    f"{self.result_path} is missing the column(s): "
                    f"{', '.join(sorted(missing))}.
"
                    f"  Found: {', '.join(sorted(headers)) or 'nothing'}
"
                    "  The file needs a header line reading exactly: phone,tps,ctps"
                )
            for row in reader:
                phone = (row.get("phone") or "").strip()
                if not phone:
                    continue
                self._rows[phone] = (
                    _normalise(row.get("tps", "")),
                    _normalise(row.get("ctps", "")),
                )

    def screen(self, phones: list[str]) -> dict[str, ScreenResult]:
        now = dt.datetime.now(dt.timezone.utc)
        out: dict[str, ScreenResult] = {}
        for phone in phones:
            tps, ctps = self._rows.get(phone, (SCREEN_ERROR, SCREEN_ERROR))
            out[phone] = ScreenResult(
                phone=phone,
                tps=tps,
                ctps=ctps,
                provider=self.name,
                checked_at=now,
                receipt=self.receipt,
            )
        return out


def export_for_screening(phones: list[str], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["phone"])
        for phone in phones:
            writer.writerow([phone])
    return path
