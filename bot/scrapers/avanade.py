"""Public Coveo Search API + Workday CXS scraper for Avanade Careers.

Discovery queries Avanade's public Coveo Search API v2 (no Playwright or login
required). Foreign (non-Brazil) roles are strictly filtered to keep ONLY roles
that are both remote AND open to LATAM.
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from bot.filters.filters import INTERNATIONAL_BLOCKED_PHRASES
from bot.scrapers.base import JobSource

logger = logging.getLogger("jobbot.scrapers.avanade")

DEFAULT_AVANADE_URL = (
    "https://www.avanade.com/en/career/search-jobs"
    "#sortCriteria=%40job_area_of_expertise%20ascending"
    "&f-jobareaofexpertise=Software%20Engineering%20AV,AI%20%26%20Data"
)

COVEO_ORG_ID = "avanadesc9productionlk3bs0x7"
COVEO_API_URL = f"https://{COVEO_ORG_ID}.org.coveo.com/rest/search/v2?organizationId={COVEO_ORG_ID}"
# Public read-only search token embedded in avanade.com/en/career/search-jobs frontend
COVEO_PUBLIC_TOKEN = "xx65a0f59b-b5a2-4db9-9e94-4542b40078fd"

WORKDAY_CXS_BASE = "https://accenture.wd103.myworkdayjobs.com/wday/cxs/accenture/AvanadeCareers"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+\n|\n{3,}")
_BRAZIL_RE = re.compile(r"\b(brazil|brasil)\b", re.IGNORECASE)
_REMOTE_RE = re.compile(
    r"\b(remote|remoto|remotely|work from home|home office|100%\s*remoto|teletrabalho|anywhere)\b",
    re.IGNORECASE,
)
_NON_REMOTE_TYPE_RE = re.compile(r"\b(hybrid|h[íi]brido|on-?site|onsite|presencial)\b", re.IGNORECASE)
_LATAM_RE = re.compile(
    r"\b(latam|latin america|am[ée]rica latina|south america|am[ée]rica do sul|"
    r"brazil|brasil|worldwide|anywhere in the world)\b",
    re.IGNORECASE,
)
_FOREIGN_ANYWHERE_RE = re.compile(
    r"\banywhere\s+in\s+(?:the\s+)?(?:u\.?s\.?a?\.?|united states|u\.?k\.?|united kingdom|canada|europe|eu|australia|india|germany|france)\b",
    re.IGNORECASE,
)
_WORKDAY_JOB_PATH_RE = re.compile(r"/AvanadeCareers(/job/[^?#]+?)(?:/apply)?(?:[?#].*)?$", re.IGNORECASE)


def _html_to_text(raw: str, max_len: int = 8000) -> str:
    if not raw:
        return ""
    text = re.sub(r"</?(?:p|div|br|li|h[1-6]|tr)[^>]*>", "\n", raw, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = _WS_RE.sub("\n\n", text)
    return text.strip()[:max_len]


def _epoch_ms_to_iso(value: int | float | str | None) -> str | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def parse_avanade_search_url(url: str) -> dict:
    """Extract Coveo facet selections and sortCriteria from an Avanade search-jobs URL."""
    frag = urllib.parse.urlparse(url or DEFAULT_AVANADE_URL).fragment
    params = urllib.parse.parse_qs(frag, keep_blank_values=False)

    def _split_csv(key: str) -> list[str]:
        out: list[str] = []
        for raw in params.get(key, []):
            for item in raw.split(","):
                cleaned = item.strip()
                if cleaned and cleaned not in out:
                    out.append(cleaned)
        return out

    areas = _split_csv("f-jobareaofexpertise") or ["Software Engineering AV", "AI & Data"]
    locations = _split_csv("f-joblocation")
    sort_criteria = (params.get("sortCriteria") or ["@job_area_of_expertise ascending"])[0]
    return {"areas": areas, "locations": locations, "sort_criteria": sort_criteria}


def is_eligible_avanade_job(
    *,
    country: str,
    location: str,
    title: str,
    body: str,
    remote_type: str = "",
) -> tuple[bool, str]:
    """Allow Brazil roles; for foreign roles require BOTH remote AND LATAM eligibility."""
    country_s = (country or "").strip()
    if _BRAZIL_RE.search(country_s):
        return True, ""

    combined = f"{country_s}\n{location or ''}\n{title or ''}\n{body or ''}"
    low = combined.lower()
    for phrase in INTERNATIONAL_BLOCKED_PHRASES:
        if phrase in low:
            return False, f"foreign restriction: {phrase}"

    if _FOREIGN_ANYWHERE_RE.search(combined):
        return False, "foreign country-restricted remote"

    rt = (remote_type or "").strip()
    if rt and _NON_REMOTE_TYPE_RE.search(rt):
        return False, f"foreign non-remote ({rt})"

    is_remote = bool(_REMOTE_RE.search(rt)) if rt else bool(_REMOTE_RE.search(combined))
    if not is_remote:
        return False, "foreign job is not remote"

    if not _LATAM_RE.search(combined):
        return False, "foreign remote job does not mention LATAM"

    return True, ""


def _infer_modality(title: str, body: str, remote_type: str = "") -> tuple[str, bool]:
    rt = (remote_type or "").strip()
    if rt:
        low_rt = rt.lower()
        if "remote" in low_rt:
            return "Remote", True
        if "hybrid" in low_rt:
            return "Hybrid", False
        if "on-site" in low_rt or "onsite" in low_rt:
            return "On-site", False
    sample = f"{title or ''}\n{(body or '')[:2000]}".lower()
    if re.search(r"\b(h[íi]brido|hybrid)\b", sample):
        return "Hybrid", False
    if re.search(r"\b(100%\s*remoto|full\s*remote|remote|remoto)\b", sample):
        return "Remote", True
    if re.search(r"\b(presencial|on-?site|onsite)\b", sample):
        return "On-site", False
    return "", False


def fetch_workday_meta(job_or_apply_url: str, timeout: int = 15) -> dict:
    """Query Avanade's public Workday CXS job detail API for remoteType and active status."""
    m = _WORKDAY_JOB_PATH_RE.search(job_or_apply_url or "")
    if not m:
        return {}
    cxs_url = f"{WORKDAY_CXS_BASE}{m.group(1)}"
    req = urllib.request.Request(
        cxs_url,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"expired": True}
        logger.debug("workday cxs http error url=%s code=%s", cxs_url, exc.code)
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("workday cxs failed url=%s err=%s", cxs_url, exc)
        return {}

    jpi = payload.get("jobPostingInfo") or {}
    if not jpi:
        return {}
    return {
        "expired": jpi.get("canApply") is False,
        "remote_type": (jpi.get("remoteType") or "").strip(),
        "location": (jpi.get("location") or "").strip(),
        "additional_locations": [str(x).strip() for x in (jpi.get("additionalLocations") or []) if str(x).strip()],
        "description": _html_to_text(jpi.get("jobDescription") or ""),
        "external_url": (jpi.get("externalUrl") or "").strip(),
    }


class AvanadeScraper(JobSource):
    """Coveo Search API scraper for Avanade Careers with foreign Remote+LATAM filtering."""

    name = "avanade"

    def __init__(
        self,
        search_url: str = DEFAULT_AVANADE_URL,
        *,
        page_size: int = 100,
        max_results: int = 400,
        verify_workday_meta: bool = True,
    ):
        self._search_url = (search_url or DEFAULT_AVANADE_URL).strip()
        self._parsed = parse_avanade_search_url(self._search_url)
        self._page_size = min(max(page_size, 1), 100)
        self._max_results = max(1, max_results)
        self._verify_workday_meta = verify_workday_meta

    def _query_coveo(self, first_result: int = 0) -> dict:
        facets = [
            {
                "facetId": "jobareaofexpertise",
                "field": "jobareaofexpertise",
                "type": "specific",
                "numberOfValues": 100,
                "currentValues": [{"value": v, "state": "selected"} for v in self._parsed["areas"]],
            }
        ]
        if self._parsed["locations"]:
            facets.append(
                {
                    "facetId": "joblocation",
                    "field": "joblocation",
                    "type": "specific",
                    "numberOfValues": 500,
                    "currentValues": [{"value": v, "state": "selected"} for v in self._parsed["locations"]],
                }
            )

        payload = {
            "locale": "en",
            "debug": False,
            "tab": "default",
            "cq": "@job_item_type == ActiveJobs AND @joblanguageid == 1 AND @isignoredjob == 0",
            "pipeline": "default_xmcloud",
            "q": "",
            "enableQuerySyntax": False,
            "searchHub": "dotcomcareersearch",
            "sortCriteria": self._parsed["sort_criteria"],
            "facets": facets,
            "numberOfResults": self._page_size,
            "firstResult": first_result,
        }
        req = urllib.request.Request(
            COVEO_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {COVEO_PUBLIC_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": UA,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def scrape(self) -> list[dict]:
        all_results: list[dict] = []
        offset = 0
        while offset < self._max_results:
            data = self._query_coveo(first_result=offset)
            batch = data.get("results") or []
            if not batch:
                break
            all_results.extend(batch)
            total = int(data.get("totalCount") or 0)
            offset += len(batch)
            if offset >= total:
                break
        return self._parse_results(all_results)

    def _parse_results(self, results: list[dict]) -> list[dict]:
        jobs: list[dict] = []
        seen: set[str] = set()
        for item in results:
            raw = item.get("raw") or {}
            job_id = str(raw.get("job_id") or raw.get("jobid") or "").strip()
            title = (raw.get("job_name") or raw.get("jobname") or item.get("title") or "").strip()
            apply_url = (raw.get("apply_url") or "").strip()
            if not title or not apply_url:
                continue
            if not job_id:
                job_id = str(item.get("urihash") or item.get("uniqueId") or title)[:24]
            stable_id = f"av:{job_id}"
            if stable_id in seen:
                continue

            country = (raw.get("jobcountry") or "").strip()
            city = (raw.get("jobcityname") or "").strip()
            location = ", ".join(p for p in (city, country) if p) or "Remote"

            area = (raw.get("jobareaofexpertise") or raw.get("job_area_of_expertise") or "").strip()
            skill_name = (raw.get("skill_name") or "").strip()
            desc_text = _html_to_text(raw.get("jobdescription") or raw.get("job_description") or "")
            qual_text = _html_to_text(raw.get("qualification") or "")

            body_parts = [f"Location: {location}"]
            if area:
                body_parts.append(f"Area of Expertise: {area}")
            if skill_name:
                body_parts.append(f"Primary Skill: {skill_name}")
            if desc_text:
                body_parts.append(desc_text)
            if qual_text:
                body_parts.append(f"Qualifications:\n{qual_text}")
            body = "\n\n".join(body_parts)[:8000]

            ok, reason = is_eligible_avanade_job(
                country=country,
                location=location,
                title=title,
                body=body,
            )
            if not ok:
                logger.debug("avanade filtered job=%s (%s): %s", stable_id, location, reason)
                continue

            # For foreign roles that passed text checks, confirm Workday remoteType is truly Remote
            remote_type = ""
            if self._verify_workday_meta and not _BRAZIL_RE.search(country):
                wd = fetch_workday_meta(apply_url)
                if wd.get("expired"):
                    continue
                remote_type = wd.get("remote_type", "")
                ok_wd, reason_wd = is_eligible_avanade_job(
                    country=country,
                    location=location,
                    title=title,
                    body=body,
                    remote_type=remote_type,
                )
                if not ok_wd:
                    logger.debug("avanade filtered after workday check job=%s: %s", stable_id, reason_wd)
                    continue

            modality, is_remote = _infer_modality(title, body, remote_type=remote_type)
            job_url = apply_url[:-6] if apply_url.lower().endswith("/apply") else apply_url

            seen.add(stable_id)
            jobs.append(
                {
                    "id": stable_id,
                    "source": self.name,
                    "url": job_url,
                    "apply_url": apply_url,
                    "title": title,
                    "company": "Avanade",
                    "location": location,
                    "modality": modality,
                    "seniority": "",
                    "remote": is_remote,
                    "salary": "",
                    "posted_at": _epoch_ms_to_iso(raw.get("jobposteddate") or raw.get("job_posted_date")),
                    "body": body,
                }
            )
        return jobs

    def fetch_detail(self, url: str) -> dict:
        """Fetch live Workday CXS status and full description for an Avanade job."""
        wd = fetch_workday_meta(url)
        if not wd:
            return {}
        if wd.get("expired"):
            return {"expired": True, "body": ""}
        parts = []
        if wd.get("remote_type"):
            parts.append(f"Work Mode: {wd['remote_type']}")
        if wd.get("location"):
            locs = [wd["location"], *(wd.get("additional_locations") or [])]
            parts.append(f"Locations: {', '.join(locs)}")
        if wd.get("description"):
            parts.append(wd["description"])
        return {
            "expired": False,
            "body": "\n\n".join(parts)[:8000],
            "apply_url": f"{url.rstrip('/')}/apply" if not url.lower().endswith("/apply") else url,
        }
