"""Public JSON/RSS scrapers for international remote developer job portals.

All sources here use official public JSON APIs or RSS feeds (no Playwright needed
during discovery) and return full job descriptions so pre-filters can discard
US-only / non-LATAM roles before AI analysis.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from bot.scrapers.base import JobSource

logger = logging.getLogger("jobbot.scrapers.international")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+\n|\n{3,}")
_ATS_URL_RE = re.compile(
    r"https?://(?:(?:job-)?boards\.greenhouse\.io|jobs\.lever\.co|jobs\.ashbyhq\.com|"
    r"apply\.workable\.com|jobs\.smartrecruiters\.com)/[^\s\"'<>]+",
    re.IGNORECASE,
)

_DEV_CATEGORY_RE = re.compile(
    r"\b(software|dev|develop|engineer|engineering|program|frontend|front-end|"
    r"backend|back-end|full[- ]?stack|web|mobile|devops|sre|cloud|data|ai|"
    r"machine learning|ml|python|javascript|typescript|react|node|tech|qa|"
    r"security|architect|platform|infrastructure)\b",
    re.IGNORECASE,
)


def _http_get(url: str, *, accept: str = "application/json", timeout: int = 25) -> str | None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.warning("http get failed url=%s err=%s", url, exc)
        return None


def _html_to_text(raw: str, max_len: int = 8000) -> str:
    if not raw:
        return ""
    text = re.sub(r"</?(?:p|div|br|li|h[1-6]|tr)[^>]*>", "\n", raw, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = _WS_RE.sub("\n\n", text)
    return text.strip()[:max_len]


def _extract_ats_url(raw_html: str) -> str:
    if not raw_html:
        return ""
    m = _ATS_URL_RE.search(html.unescape(raw_html))
    return m.group(0).rstrip(".,);") if m else ""


def _is_dev_category(*values: str) -> bool:
    combined = " ".join(v for v in values if v).strip()
    if not combined:
        return True
    return bool(_DEV_CATEGORY_RE.search(combined))


def _stable_id(prefix: str, raw: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "", str(raw or ""))
    if clean and len(clean) <= 32:
        return f"{prefix}:{clean}"
    return f"{prefix}:{hashlib.sha1(str(raw).encode('utf-8')).hexdigest()[:16]}"


def _parse_pubdate_rfc2822(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return None


def _parse_unix_timestamp(value: int | float | str | None) -> str | None:
    if value in (None, ""):
        return None
    try:
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return None


class RemotiveScraper(JobSource):
    """Public JSON API scraper for Remotive (remotive.com/api/remote-jobs)."""

    name = "remotive"
    api_url = "https://remotive.com/api/remote-jobs?category=software-dev"

    def __init__(self, limit: int = 50):
        self._limit = limit

    def scrape(self) -> list[dict]:
        raw = _http_get(f"{self.api_url}&limit={self._limit}")
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("remotive invalid json: %s", exc)
            return []
        return self._parse_items(payload.get("jobs") or [])

    def _parse_items(self, items: list[dict]) -> list[dict]:
        out: list[dict] = []
        for item in items:
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            if not title or not url:
                continue
            category = (item.get("category") or "").strip()
            tags = [str(t).strip() for t in (item.get("tags") or []) if str(t).strip()]
            if category and not _is_dev_category(category, title):
                continue
            desc_html = item.get("description") or ""
            apply_url = _extract_ats_url(desc_html) or url
            body_parts = []
            if tags:
                body_parts.append(f"Tags: {', '.join(tags)}")
            if item.get("candidate_required_location"):
                body_parts.append(f"Location: {item['candidate_required_location']}")
            if item.get("job_type"):
                body_parts.append(f"Type: {item['job_type']}")
            body_parts.append(_html_to_text(desc_html))
            out.append(
                {
                    "source": self.name,
                    "id": _stable_id("rm", item.get("id") or url),
                    "url": url,
                    "apply_url": apply_url,
                    "title": title,
                    "company": (item.get("company_name") or "").strip(),
                    "location": (item.get("candidate_required_location") or "Worldwide").strip(),
                    "modality": "Remote",
                    "seniority": "",
                    "remote": True,
                    "salary": (item.get("salary") or "").strip(),
                    "posted_at": item.get("publication_date"),
                    "body": "\n".join(p for p in body_parts if p)[:8000],
                }
            )
        return out


class HimalayasScraper(JobSource):
    """Public JSON API scraper for Himalayas (himalayas.app/jobs/api)."""

    name = "himalayas"
    api_url = "https://himalayas.app/jobs/api"

    def __init__(self, limit: int = 50):
        self._limit = min(max(limit, 1), 100)

    def scrape(self) -> list[dict]:
        raw = _http_get(f"{self.api_url}?limit={self._limit}")
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("himalayas invalid json: %s", exc)
            return []
        return self._parse_items(payload.get("jobs") or [])

    def _parse_items(self, items: list[dict]) -> list[dict]:
        out: list[dict] = []
        for item in items:
            title = (item.get("title") or "").strip()
            guid = (item.get("guid") or "").strip()
            app_link = (item.get("applicationLink") or "").strip()
            url = guid or app_link
            if not title or not url:
                continue
            parent_cats = " ".join(str(c) for c in (item.get("parentCategories") or []))
            cats = " ".join(str(c) for c in (item.get("categories") or []))
            if not _is_dev_category(parent_cats, cats, title):
                continue

            loc_restrictions = [str(x).strip() for x in (item.get("locationRestrictions") or []) if str(x).strip()]
            location = ", ".join(loc_restrictions) if loc_restrictions else "Worldwide"
            tz_restrictions = item.get("timezoneRestrictions") or []

            min_sal = item.get("minSalary")
            max_sal = item.get("maxSalary")
            currency = (item.get("currency") or "USD").strip()
            period = (item.get("salaryPeriod") or "").strip()
            salary = ""
            if min_sal and max_sal:
                salary = f"{currency} {min_sal}-{max_sal} {period}".strip()
            elif min_sal:
                salary = f"{currency} {min_sal}+ {period}".strip()

            seniority_list = item.get("seniority") or []
            seniority = ", ".join(str(s) for s in seniority_list) if isinstance(seniority_list, list) else str(seniority_list)

            desc_html = item.get("description") or item.get("excerpt") or ""
            apply_url = _extract_ats_url(desc_html) or app_link or url
            body_parts = [f"Location: {location}"]
            if tz_restrictions:
                body_parts.append(f"Timezones (UTC): {', '.join(str(t) for t in tz_restrictions)}")
            if item.get("employmentType"):
                body_parts.append(f"Employment: {item['employmentType']}")
            body_parts.append(_html_to_text(desc_html))

            out.append(
                {
                    "source": self.name,
                    "id": _stable_id("hm", guid or url),
                    "url": url,
                    "apply_url": apply_url,
                    "title": title,
                    "company": (item.get("companyName") or "").strip(),
                    "location": location,
                    "modality": "Remote",
                    "seniority": seniority.strip(),
                    "remote": True,
                    "salary": salary,
                    "posted_at": _parse_unix_timestamp(item.get("pubDate")),
                    "body": "\n".join(p for p in body_parts if p)[:8000],
                }
            )
        return out


class WeWorkRemotelyScraper(JobSource):
    """Public RSS feed scraper for We Work Remotely programming jobs."""

    name = "weworkremotely"
    feed_url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"

    def scrape(self) -> list[dict]:
        raw = _http_get(self.feed_url, accept="application/rss+xml, application/xml, text/xml")
        if not raw:
            return []
        return self._parse_rss(raw)

    def _parse_rss(self, xml_text: str) -> list[dict]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.warning("wwr invalid xml: %s", exc)
            return []

        out: list[dict] = []
        for item in root.findall("./channel/item"):
            raw_title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or item.findtext("guid") or "").strip()
            if not raw_title or not link:
                continue

            if ":" in raw_title:
                company, title = [p.strip() for p in raw_title.split(":", 1)]
            else:
                company, title = "", raw_title

            region = (item.findtext("region") or "Anywhere in the World").strip()
            category = (item.findtext("category") or "").strip()
            desc_html = item.findtext("description") or ""
            apply_url = _extract_ats_url(desc_html) or link

            body_parts = [f"Region: {region}"]
            if category:
                body_parts.append(f"Category: {category}")
            body_parts.append(_html_to_text(desc_html))

            out.append(
                {
                    "source": self.name,
                    "id": _stable_id("wwr", item.findtext("guid") or link),
                    "url": link,
                    "apply_url": apply_url,
                    "title": title,
                    "company": company,
                    "location": region,
                    "modality": "Remote",
                    "seniority": "",
                    "remote": True,
                    "salary": "",
                    "posted_at": _parse_pubdate_rfc2822(item.findtext("pubDate")),
                    "body": "\n".join(p for p in body_parts if p)[:8000],
                }
            )
        return out


class JobicyScraper(JobSource):
    """Public JSON API v2 scraper for Jobicy (jobicy.com/api/v2/remote-jobs)."""

    name = "jobicy"
    api_url = "https://jobicy.com/api/v2/remote-jobs"

    def __init__(self, geos: list[str] | None = None, count: int = 50):
        self._geos = geos or ["latam", "anywhere"]
        self._count = min(max(count, 1), 50)

    def scrape(self) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        for geo in self._geos:
            qs = urllib.parse.urlencode({"count": self._count, "geo": geo})
            raw = _http_get(f"{self.api_url}?{qs}")
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("jobicy invalid json geo=%s: %s", geo, exc)
                continue
            for job in self._parse_items(payload.get("jobs") or []):
                if job["id"] not in seen:
                    seen.add(job["id"])
                    out.append(job)
        return out

    def _parse_items(self, items: list[dict]) -> list[dict]:
        out: list[dict] = []
        for item in items:
            title = html.unescape((item.get("jobTitle") or "").strip())
            url = (item.get("url") or "").strip()
            if not title or not url:
                continue
            industries = " ".join(str(i) for i in (item.get("jobIndustry") or []))
            if not _is_dev_category(industries, title):
                continue

            min_sal = item.get("salaryMin")
            max_sal = item.get("salaryMax")
            currency = (item.get("salaryCurrency") or "USD").strip()
            period = (item.get("salaryPeriod") or "").strip()
            salary = ""
            if min_sal and max_sal:
                salary = f"{currency} {min_sal}-{max_sal} {period}".strip()
            elif min_sal:
                salary = f"{currency} {min_sal}+ {period}".strip()

            desc_html = item.get("jobDescription") or item.get("jobExcerpt") or ""
            apply_url = _extract_ats_url(desc_html) or url
            location = (item.get("jobGeo") or "Anywhere").strip()
            job_type = ", ".join(str(t) for t in (item.get("jobType") or []))
            body_parts = [f"Location: {location}"]
            if job_type:
                body_parts.append(f"Type: {job_type}")
            body_parts.append(_html_to_text(desc_html))

            out.append(
                {
                    "source": self.name,
                    "id": _stable_id("jc", item.get("id") or url),
                    "url": url,
                    "apply_url": apply_url,
                    "title": title,
                    "company": html.unescape((item.get("companyName") or "").strip()),
                    "location": location,
                    "modality": "Remote",
                    "seniority": (item.get("jobLevel") or "").strip(),
                    "remote": True,
                    "salary": salary,
                    "posted_at": item.get("pubDate"),
                    "body": "\n".join(p for p in body_parts if p)[:8000],
                }
            )
        return out


class WorkingNomadsScraper(JobSource):
    """Public JSON API scraper for Working Nomads (workingnomads.com/api/exposed_jobs/)."""

    name = "workingnomads"
    api_url = "https://www.workingnomads.com/api/exposed_jobs/"

    def scrape(self) -> list[dict]:
        raw = _http_get(self.api_url)
        if not raw:
            return []
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("workingnomads invalid json: %s", exc)
            return []
        if not isinstance(items, list):
            return []
        return self._parse_items(items)

    def _parse_items(self, items: list[dict]) -> list[dict]:
        out: list[dict] = []
        for item in items:
            title = html.unescape((item.get("title") or "").strip())
            url = (item.get("url") or "").strip()
            if not title or not url:
                continue
            category = (item.get("category_name") or "").strip()
            if category and not _is_dev_category(category, title):
                continue

            desc_html = item.get("description") or ""
            apply_url = _extract_ats_url(desc_html) or url
            location = (item.get("location") or "Anywhere").strip()
            tags = (item.get("tags") or "").strip()
            body_parts = [f"Location: {location}"]
            if tags:
                body_parts.append(f"Tags: {tags}")
            body_parts.append(_html_to_text(desc_html))

            out.append(
                {
                    "source": self.name,
                    "id": _stable_id("wn", url),
                    "url": url,
                    "apply_url": apply_url,
                    "title": title,
                    "company": html.unescape((item.get("company_name") or "").strip()),
                    "location": location,
                    "modality": "Remote",
                    "seniority": "",
                    "remote": True,
                    "salary": "",
                    "posted_at": item.get("pub_date"),
                    "body": "\n".join(p for p in body_parts if p)[:8000],
                }
            )
        return out
