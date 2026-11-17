"""Company website discovery and verification.

Deliberately conservative. The rule from the protocol is that a logo or a
similar name is not enough: we only call a website LINKED when the site itself
carries the company number, which UK companies must publish. Everything softer
stays CANDIDATE and waits for a human.

There is no search engine here on purpose. Guessing domains from the company
name and then proving or disproving each guess is cheaper, has no API cost,
and produces an auditable reason either way.

Email addresses are only ever read off the page. This module never invents
firstname.lastname@ patterns.
"""

from __future__ import annotations

import datetime as dt
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from app.config import settings

# Suffixes stripped before turning a legal name into candidate domains.
_LEGAL_SUFFIXES = (
    "limited", "ltd", "llp", "plc", "company", "co", "uk", "group",
    "holdings", "services", "the",
)
_TLDS = (".co.uk", ".com", ".uk", ".ltd.uk", ".net")
_CONTACT_PATHS = ("", "/contact", "/contact-us", "/contact-us/", "/about", "/about-us")

# UK numbers as they actually appear on websites: 01xxx, 02x, 03xx, 07xxx,
# with spaces, dashes or brackets, optionally +44.
_PHONE_RE = re.compile(
    r"(?:(?:\+|00)\s?44\s?|0)(?:\d\s?){9,10}\d"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_COMPANY_NO_RE = re.compile(r"\b(?:0?\d{7}|[A-Z]{2}\d{6})\b")

# Addresses that belong to a platform, not to the company.
_EMAIL_BLOCKLIST = (
    "sentry.io", "wixpress.com", "example.com", "godaddy.com", "squarespace.com",
    "wordpress.com", "sentry-next", "domain.com", "yourdomain",
)


@dataclass
class SiteFinding:
    url: str
    reachable: bool
    company_number_found: bool = False
    postcode_found: bool = False
    legal_name_found: bool = False
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    pages_checked: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def match_status(self) -> str:
        """LINKED only when the site states the company number itself."""
        if self.company_number_found:
            return "LINKED"
        if self.postcode_found or self.legal_name_found:
            return "CANDIDATE"
        return "UNKNOWN"

    @property
    def rationale(self) -> str:
        bits = []
        if self.company_number_found:
            bits.append("company number published on site")
        if self.postcode_found:
            bits.append("registered postcode matches site")
        if self.legal_name_found:
            bits.append("legal name appears on site")
        if not bits:
            bits.append("no corroborating identifier found on site")
        return "; ".join(bits) + f" (pages checked: {len(self.pages_checked)})"


def normalise_name(name: str) -> list[str]:
    """Turn a legal name into the word stem a domain is likely built from."""
    cleaned = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    words = [w for w in cleaned.split() if w and w not in _LEGAL_SUFFIXES]
    if not words:
        return []
    joined = "".join(words)
    stems = [joined]
    if len(words) > 1:
        stems.append("-".join(words))
        stems.append("".join(words[:2]))
    # Deduplicate, keep order, drop anything implausibly short.
    seen: list[str] = []
    for stem in stems:
        if len(stem) >= 4 and stem not in seen:
            seen.append(stem)
    return seen


def candidate_domains(legal_name: str, limit: int = 8) -> list[str]:
    out: list[str] = []
    for stem in normalise_name(legal_name):
        for tld in _TLDS:
            candidate = f"{stem}{tld}"
            if candidate not in out:
                out.append(candidate)
            if len(out) >= limit:
                return out
    return out


def _normalise_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("+44"):
        digits = "0" + digits[3:]
    elif digits.startswith("0044"):
        digits = "0" + digits[4:]
    elif digits.startswith("44") and len(digits) >= 12:
        digits = "0" + digits[2:]
    if not digits.startswith("0"):
        return None
    if len(digits) not in (10, 11):
        return None
    # 0800 and similar are service numbers, still fine. 09 premium is not a
    # business contact number worth calling.
    if digits.startswith("09"):
        return None
    return digits


def _clean_emails(found: list[str], host: str) -> list[str]:
    out: list[str] = []
    bare_host = host.replace("www.", "")
    for email in found:
        lowered = email.lower().strip(".,;:)")
        if any(bad in lowered for bad in _EMAIL_BLOCKLIST):
            continue
        if lowered.endswith((".png", ".jpg", ".gif", ".webp", ".svg")):
            continue
        domain = lowered.split("@")[-1]
        # Only keep mailboxes on the company's own domain. An address on
        # someone else's domain proves nothing about this company.
        if bare_host and bare_host not in domain:
            continue
        if lowered not in out:
            out.append(lowered)
    return out


class WebsiteProbe:
    """Fetches a handful of pages per domain, politely, and reads what is there."""

    def __init__(self, delay: float | None = None) -> None:
        self.delay = settings.crawl_delay_seconds if delay is None else delay
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=15.0,
            follow_redirects=True,
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "WebsiteProbe":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        if root not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(urljoin(root, "/robots.txt"))
            try:
                response = self._client.get(urljoin(root, "/robots.txt"))
                if response.status_code == 200:
                    parser.parse(response.text.splitlines())
                else:
                    parser = None  # no robots.txt means no restriction
            except httpx.HTTPError:
                parser = None
            self._robots[root] = parser
        parser = self._robots[root]
        if parser is None:
            return True
        return parser.can_fetch(settings.user_agent, url)

    def probe(
        self,
        domain: str,
        company_number: str,
        legal_name: str,
        postcode: str | None,
    ) -> SiteFinding:
        base = domain if domain.startswith("http") else f"https://{domain}"
        finding = SiteFinding(url=base, reachable=False)

        number_variants = {
            company_number,
            company_number.lstrip("0"),
            company_number.zfill(8),
        }
        name_stem = re.sub(r"[^a-z0-9]", "", legal_name.lower())[:20]
        postcode_squashed = (
            re.sub(r"\s+", "", postcode).upper() if postcode else None
        )

        phones: list[str] = []
        emails: list[str] = []

        for index, path in enumerate(_CONTACT_PATHS):
            url = urljoin(base + "/", path.lstrip("/")) if path else base
            if not self._allowed(url):
                continue
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                if not finding.pages_checked:
                    finding.error = f"{type(exc).__name__}: {exc}"
                time.sleep(self.delay)
                # _CONTACT_PATHS[0] is the site root. If that does not resolve,
                # the domain is not in use and its /contact page will not be
                # either. Abandoning here turns a dead candidate domain from
                # six polite waits into one, which is the difference between
                # minutes and seconds per company across a batch.
                if index == 0:
                    break
                continue

            time.sleep(self.delay)

            if response.status_code >= 400:
                continue

            finding.reachable = True
            finding.pages_checked.append(str(response.url))

            tree = HTMLParser(response.text)
            for tag in tree.css("script, style, noscript"):
                tag.decompose()
            text = tree.text(separator=" ")
            squashed = re.sub(r"\s+", "", text).upper()

            for variant in number_variants:
                if variant and variant in re.sub(r"[^0-9A-Z]", "", squashed):
                    finding.company_number_found = True
                    break
            if postcode_squashed and postcode_squashed in squashed:
                finding.postcode_found = True
            if name_stem and name_stem in re.sub(r"[^a-z0-9]", "", text.lower()):
                finding.legal_name_found = True

            # tel: and mailto: links are the most reliable, take them first.
            for node in tree.css("a[href^='tel:']"):
                raw = (node.attributes.get("href") or "")[4:]
                number = _normalise_phone(raw)
                if number and number not in phones:
                    phones.append(number)
            for node in tree.css("a[href^='mailto:']"):
                raw = (node.attributes.get("href") or "")[7:].split("?")[0]
                if raw and raw not in emails:
                    emails.append(raw)

            for raw in _PHONE_RE.findall(text):
                number = _normalise_phone(raw)
                if number and number not in phones:
                    phones.append(number)
            emails.extend(_EMAIL_RE.findall(text))

            # One page with the company number is proof enough. Stop early.
            if finding.company_number_found and phones:
                break

        host = urlparse(base).netloc
        finding.phones = phones[:3]
        finding.emails = _clean_emails(emails, host)[:3]
        return finding


def find_website(
    legal_name: str,
    company_number: str,
    postcode: str | None,
    probe: WebsiteProbe,
    max_candidates: int = 6,
) -> SiteFinding | None:
    """Try candidate domains in order, return the first that corroborates.

    Returns the best finding seen, or None when nothing was reachable.
    """
    best: SiteFinding | None = None
    for domain in candidate_domains(legal_name, limit=max_candidates):
        finding = probe.probe(domain, company_number, legal_name, postcode)
        if not finding.reachable:
            continue
        if finding.company_number_found:
            return finding
        if best is None or finding.match_status == "CANDIDATE":
            best = finding
    return best


def today() -> dt.date:
    return dt.date.today()