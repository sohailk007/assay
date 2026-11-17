"""Companies House public API client.

Free, official, and the only source in this project that can establish legal
identity. It cannot tell you a trading website, a marketing number or a
training need, and this module does not pretend otherwise.

Auth is HTTP Basic with the API key as the username and an empty password.
The published rate limit is 600 requests per five minutes, so the client
paces itself rather than finding out the hard way.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field

import httpx

from app.config import settings

# Statuses that should stop research. "active-proposal-to-strike-off" is the
# one that matters most: the company is technically live but on its way out.
ADVERSE_STATUSES = {
    "dissolved": "Dissolved",
    "liquidation": "In liquidation",
    "receivership": "In receivership",
    "administration": "In administration",
    "voluntary-arrangement": "Voluntary arrangement",
    "converted-closed": "Converted or closed",
    "insolvency-proceedings": "Insolvency proceedings",
    "closed": "Closed",
}


@dataclass
class CompanyRecord:
    company_number: str
    legal_name: str
    company_status: str | None
    company_type: str | None
    date_of_creation: dt.date | None
    sic_codes: list[str] = field(default_factory=list)
    registered_locality: str | None = None
    registered_postcode: str | None = None

    @property
    def adverse(self) -> tuple[bool, str | None]:
        status = (self.company_status or "").lower()
        if status in ADVERSE_STATUSES:
            return True, ADVERSE_STATUSES[status]
        if "strike-off" in status or "strike off" in status:
            return True, "Active proposal to strike off"
        if not status:
            return True, "Status unknown"
        return False, None


class RateLimiter:
    """Simple sliding window. 600 calls per 300 seconds by default."""

    def __init__(self, max_calls: int = 550, window_seconds: float = 300.0) -> None:
        self.max_calls = max_calls
        self.window = window_seconds
        self._calls: list[float] = []

    def wait(self) -> None:
        now = time.monotonic()
        self._calls = [t for t in self._calls if now - t < self.window]
        if len(self._calls) >= self.max_calls:
            sleep_for = self.window - (now - self._calls[0]) + 0.5
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._calls = []
        self._calls.append(time.monotonic())


class CompaniesHouseError(RuntimeError):
    pass


class CompaniesHouseClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.companies_house_api_key
        if not self.api_key:
            raise CompaniesHouseError(
                "COMPANIES_HOUSE_API_KEY is not set. Register a REST API key at "
                "https://developer.company-information.service.gov.uk/ and put it "
                "in your .env file."
            )
        self._limiter = RateLimiter()
        self._client = httpx.Client(
            base_url=settings.companies_house_base_url,
            auth=(self.api_key, ""),
            headers={"Accept": "application/json", "User-Agent": settings.user_agent},
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CompaniesHouseClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(4):
            self._limiter.wait()
            response = self._client.get(path, params=params)
            if response.status_code == 429:
                # Back off and let the window drain.
                time.sleep(10 * (attempt + 1))
                continue
            if response.status_code == 404:
                return {}
            if response.status_code == 401:
                raise CompaniesHouseError(
                    "Companies House rejected the API key (401). Check the key is a "
                    "REST key, not a stream key, and that it is active."
                )
            response.raise_for_status()
            return response.json()
        raise CompaniesHouseError("Rate limited by Companies House after 4 attempts.")

    def advanced_search(
        self,
        sic_codes: list[str],
        location: str | None = None,
        company_status: list[str] | None = None,
        incorporated_from: dt.date | None = None,
        incorporated_to: dt.date | None = None,
        limit: int = 100,
    ) -> list[CompanyRecord]:
        """Search the register by SIC code and location.

        location matches the registered office address, which is not where the
        company trades. Treat it as a rough geography filter and nothing more.
        """
        results: list[CompanyRecord] = []
        start_index = 0
        page_size = 100

        while len(results) < limit:
            params: dict[str, object] = {
                "sic_codes": ",".join(sic_codes),
                "size": min(page_size, limit - len(results)),
                "start_index": start_index,
            }
            if location:
                params["location"] = location
            if company_status:
                params["company_status"] = ",".join(company_status)
            if incorporated_from:
                params["incorporated_from"] = incorporated_from.isoformat()
            if incorporated_to:
                params["incorporated_to"] = incorporated_to.isoformat()

            payload = self._get("/advanced-search/companies", params=params)
            items = payload.get("items") or []
            if not items:
                break

            for item in items:
                record = _parse_search_item(item)
                if record:
                    results.append(record)

            start_index += len(items)
            if start_index >= int(payload.get("hits", 0) or 0):
                break

        return results[:limit]

    def get_company(self, company_number: str) -> CompanyRecord | None:
        """Fetch one live company profile. Use this to re-check status."""
        payload = self._get(f"/company/{company_number}")
        if not payload:
            return None
        return _parse_profile(company_number, payload)


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _parse_search_item(item: dict) -> CompanyRecord | None:
    number = item.get("company_number")
    name = item.get("company_name")
    if not number or not name:
        return None
    address = item.get("registered_office_address") or {}
    return CompanyRecord(
        company_number=str(number).strip().upper(),
        legal_name=str(name).strip(),
        company_status=item.get("company_status"),
        company_type=item.get("company_type"),
        date_of_creation=_parse_date(item.get("date_of_creation")),
        sic_codes=[str(c) for c in (item.get("sic_codes") or [])],
        registered_locality=address.get("locality") or address.get("region"),
        registered_postcode=address.get("postal_code"),
    )


def _parse_profile(company_number: str, payload: dict) -> CompanyRecord:
    address = payload.get("registered_office_address") or {}
    return CompanyRecord(
        company_number=company_number.strip().upper(),
        legal_name=str(payload.get("company_name") or "").strip(),
        company_status=payload.get("company_status"),
        company_type=payload.get("type"),
        date_of_creation=_parse_date(payload.get("date_of_creation")),
        sic_codes=[str(c) for c in (payload.get("sic_codes") or [])],
        registered_locality=address.get("locality") or address.get("region"),
        registered_postcode=address.get("postal_code"),
    )
