"""The screening contract.

Every provider implements one method. Swapping tpsmydata for Data Soap or
TPSClear is a new file in this folder and one line in .env, nothing else.

Two rules that the rest of the system relies on:

1. A result is a point-in-time fact with a receipt. It is never cached
   indefinitely and never inferred. If the provider errors, the answer is
   ERROR, which the call gate treats exactly like REGISTERED: do not call.
2. CLEAR means "not on the register when we asked". It does not mean the
   person wants to hear from you, and it does not override a prior objection
   or the internal do-not-contact list.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.models import SCREEN_CLEAR, SCREEN_ERROR, SCREEN_NOT_RUN, SCREEN_REGISTERED

__all__ = [
    "ScreenResult",
    "ScreeningProvider",
    "SCREEN_CLEAR",
    "SCREEN_ERROR",
    "SCREEN_NOT_RUN",
    "SCREEN_REGISTERED",
]


@dataclass
class ScreenResult:
    phone: str
    tps: str = SCREEN_NOT_RUN
    ctps: str = SCREEN_NOT_RUN
    provider: str = "unknown"
    checked_at: dt.datetime = field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )
    receipt: str | None = None

    @property
    def clear(self) -> bool:
        return self.tps == SCREEN_CLEAR and self.ctps == SCREEN_CLEAR

    @property
    def summary(self) -> str:
        return f"TPS={self.tps} CTPS={self.ctps} via {self.provider}"


class ScreeningProvider(ABC):
    name: str = "base"

    @abstractmethod
    def screen(self, phones: list[str]) -> dict[str, ScreenResult]:
        """Screen a batch of UK numbers. Keyed by the number as passed in."""

    def close(self) -> None:
        return None
