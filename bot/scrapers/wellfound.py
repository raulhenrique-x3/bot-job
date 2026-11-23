"""Playwright scraper for Wellfound (formerly AngelList Talent).

Uses the merged authenticated Playwright session (`state/auth_wellfound.json`,
created via `.venv/bin/python -m bot.login wellfound`). Extracts listings from
Next.js `__NEXT_DATA__` Apollo state when available, with a DOM link fallback.
Never attempts to bypass CAPTCHA/DataDome/Cloudflare gates.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from bot.config import env_list
from bot.scrapers.base import JobSource
from bot.scrapers.browser import goto, polite_sleep

logger = logging.getLogger("jobbot.scrapers.wellfound")

BASE = "https://wellfound.com"
DEFAULT_WELLFOUND_URLS: tuple[str, ...] = (
    "https://wellfound.com/role/r/full-stack-engineer",
    "https://wellfound.com/role/r/software-engineer",
    "https://wellfound.com/role/r/backend-engineer",
)

_JOB_URL_RE = re.compile(r"/(?:company/[^/]+/)?jobs/(\d+)(?:-[^/?#]+)?", re.IGNORECASE)
_AUTH_PATH_RE = re.compile(r"^/(?:login|signup|sign_in|onboarding)(?:/|$)", re.IGNORECASE)
_CHALLENGE_RE = re.compile(
    r"captcha-delivery\.com|datadome|just a moment\.\.\.|verify you are human|cf-turnstile|attention required",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(raw: str, max_len: int = 8000) -> str:
    if not raw:
        return ""
    text = re.sub(r"</?(?:p|div|br|li|h[1-6]|tr)[^>]*>", "\n", raw, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_len]


def _stable_wf_id(raw_id: str | int) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "", str(raw_id or ""))
    if clean and len(clean) <= 32:
        return f"wf:{clean}"
    return f"wf:{hashlib.sha1(str(raw_id).encode('utf-8')).hexdigest()[:16]}"


def _normalize_posted_at(value: int | float | str | None) -> str | None:
    """Normalize Wellfound's liveStartAt (Unix seconds/ms or ISO string) to UTC ISO-8601."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            if ts <= 0:
                return None
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, TypeError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            return _normalize_posted_at(float(raw))
        return raw
    return None


def _is_challenge_page(page_html: str) -> bool:
    if not page_html:
        return False
    return bool(_CHALLENGE_RE.search(page_html[:6000]))


def _is_auth_redirect(url: str) -> bool:
    path = urlparse(url or "").path or ""
    return bool(_AUTH_PATH_RE.search(path))


class WellfoundScraper(JobSource):
    name = "wellfound"

    def __init__(self, browser, urls: list[str] | None = None):
        self._browser = browser
        configured = urls if urls is not None else env_list("WELLFOUND_URLS", "")
        self._urls = [u.strip() for u in configured if u.strip()] or list(DEFAULT_WELLFOUND_URLS)
        self.session_lost = False

    def scrape(self) -> list[dict]:
        if self._browser is None:
            return []
        jobs: list[dict] = []
        seen: set[str] = set()
        for url in self._urls:
            page_html = self._fetch_html(url)
            if not page_html:
                if self.session_lost:
                    break
                continue
            found = self._parse_listing(page_html)
            for job in found:
                if job["id"] not in seen:
                    seen.add(job["id"])
                    jobs.append(job)
            polite_sleep(1.5, 3.0)
        return jobs

    def _fetch_html(self, url: str) -> str:
        with goto(self._browser, url, timeout_ms=45000) as page:
            if page is None:
                return ""
            current_url = page.url or url
            if _is_auth_redirect(current_url):
                self.session_lost = True
                logger.warning(
                    "Wellfound session missing or expired (redirected to %s). "
                    "Run: .venv/bin/python -m bot.login wellfound",
                    current_url,
                )
                return ""
            content = page.content()
            if _is_challenge_page(content):
                logger.warning("Wellfound anti-bot/CAPTCHA gate detected on %s — skipping safely.", current_url)
                return ""
            return content

    def _parse_listing(self, page_html: str) -> list[dict]:
        jobs = self._parse_next_data(page_html)
        if jobs:
            return jobs
        return self._parse_dom_fallback(page_html)

    def _parse_next_data(self, page_html: str) -> list[dict]:
        """Extract JobListing objects from Wellfound's Next.js Apollo state."""
        soup = BeautifulSoup(page_html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return []
        try:
            payload = json.loads(script.string)
        except json.JSONDecodeError:
            return []

        apollo = (
            payload.get("props", {}).get("pageProps", {}).get("apolloState", {}).get("data")
            or payload.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__")
            or {}
        )
        if not isinstance(apollo, dict):
            return []

        # Map StartupResult / Startup refs -> company name & slug
        startups_by_ref: dict[str, dict] = {}
        for key, val in apollo.items():
            if not isinstance(val, dict):
                continue
            typename = str(val.get("__typename") or "")
            if "Startup" in typename or key.startswith("StartupResult:"):
                startups_by_ref[key] = val

        # Build reverse map from job listing ref -> startup info
        startup_for_job: dict[str, dict] = {}
        for skey, sval in startups_by_ref.items():
            highlighted = sval.get("highlightedJobListings") or sval.get("jobListings") or []
            if isinstance(highlighted, list):
                for item in highlighted:
                    ref = item.get("__ref") if isinstance(item, dict) else None
                    if ref:
                        startup_for_job[ref] = sval

        jobs: list[dict] = []
        seen: set[str] = set()
        for key, val in apollo.items():
            if not isinstance(val, dict):
                continue
            typename = str(val.get("__typename") or "")
            if typename not in ("JobListing", "JobListingSearchResult") and not key.startswith("JobListing"):
                continue

            title = (val.get("title") or "").strip()
            raw_id = val.get("id") or val.get("slug") or key.split(":")[-1]
            if not title or not raw_id:
                continue

            jid = _stable_wf_id(raw_id)
            if jid in seen:
                continue
            seen.add(jid)

            startup = startup_for_job.get(key) or {}
            if not startup and isinstance(val.get("startup"), dict):
                ref = val["startup"].get("__ref")
                startup = startups_by_ref.get(ref, val["startup"]) if ref else val["startup"]

            company = (startup.get("name") or val.get("companyName") or "").strip()
            company_slug = (startup.get("slug") or "").strip()
            job_slug = (val.get("slug") or "").strip()

            if company_slug and job_slug:
                job_url = f"{BASE}/company/{company_slug}/jobs/{job_slug}"
            elif job_slug:
                job_url = f"{BASE}/jobs/{job_slug}"
            else:
                job_url = f"{BASE}/jobs/{raw_id}"

            loc_names = val.get("locationNames") or []
            if isinstance(loc_names, list):
                location = ", ".join(str(x).strip() for x in loc_names if str(x).strip())
            else:
                location = str(loc_names or "").strip()
            remote = bool(val.get("remote")) or "remote" in location.lower()
            if remote and not location:
                location = "Remote"

            compensation = (val.get("compensation") or "").strip()
            raw_desc = val.get("description") or val.get("descriptionHtml") or ""
            body_parts = [_clean_html(raw_desc)] if raw_desc else []
            if compensation:
                body_parts.insert(0, f"Compensation: {compensation}")
            if location:
                body_parts.insert(0, f"Location: {location}")

            jobs.append({
                "id": jid,
                "source": self.name,
                "title": title,
                "company": company,
                "url": job_url,
                "apply_url": job_url,
                "location": location or "Remote",
                "remote": remote,
                "salary": compensation or None,
                "body": "\n".join(p for p in body_parts if p),
                "posted_at": _normalize_posted_at(val.get("liveStartAt")),
            })
        return jobs

    def _parse_dom_fallback(self, page_html: str) -> list[dict]:
        """Fallback DOM parser for rendered Wellfound job listing anchors."""
        soup = BeautifulSoup(page_html, "html.parser")
        jobs: list[dict] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            m = _JOB_URL_RE.search(href)
            if not m:
                continue
            raw_id = m.group(1)
            jid = _stable_wf_id(raw_id)
            if jid in seen:
                continue
            title = a.get_text(" ", strip=True)
            if not title or len(title) < 3 or title.lower() in ("apply", "learn more", "view job"):
                continue
            seen.add(jid)
            full_url = urljoin(BASE, href.split("?")[0])
            # Try to find nearby company name from URL slug or parent container
            company = ""
            cm = re.search(r"/company/([^/]+)/jobs/", full_url)
            if cm:
                company = cm.group(1).replace("-", " ").title()
            jobs.append({
                "id": jid,
                "source": self.name,
                "title": title,
                "company": company,
                "url": full_url,
                "apply_url": full_url,
                "location": "Remote",
                "remote": True,
                "salary": None,
                "body": "",
                "posted_at": None,
            })
        return jobs

    def fetch_details(self, url: str) -> dict:
        if self._browser is None or not url:
            return {}
        page_html = self._fetch_html(url)
        if not page_html:
            return {}
        parsed = self._parse_next_data(page_html)
        for item in parsed:
            if item.get("body") and len(item["body"]) > 80:
                return {"body": item["body"], "location": item.get("location", "")}
        soup = BeautifulSoup(page_html, "html.parser")
        main = soup.find("main") or soup.find("body")
        if not main:
            return {}
        text = main.get_text("\n", strip=True)
        return {"body": text[:8000]}
