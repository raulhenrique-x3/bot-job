"""Public JSON API scraper for InHire career portals (*.inhire.app via api.inhire.app)."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable
from urllib.parse import urlparse

from bot.appliers.inhire import INHIRE_API_BASE, parse_inhire_url
from bot.config import env_list
from bot.scrapers.base import JobSource
from bot.scrapers.international import _html_to_text, _is_dev_category

logger = logging.getLogger("jobbot.scrapers.inhire")

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_RESERVED_SUBDOMAINS = frozenset({
    "api", "auth", "form-app", "forms", "forms-dev", "forms-prod", "embed", "files", "www", "mcp",
})
_REMOTE_TEXT_RE = re.compile(r"\b(?:remot[oa]?|home\s*office)\b", re.IGNORECASE)

# Active Brazilian tech companies and consultancies verified on api.inhire.app
DEFAULT_INHIRE_TENANTS: tuple[str, ...] = (
    "radix",
    "mjv",
    "alun",
    "sharepeoplehub",
    "db1",
    "contabilizei",
    "mottu",
    "magazineluiza",
    "loggi",
    "qitech",
    "dock",
    "lyncas",
    "byx",
    "cielo",
    "xp",
    "alelo",
    "turbi",
    "olist",
    "bmp",
    "extractta",
    "neoway",
    "cubos",
    "contaazul",
    "qive",
    "alice",
    "appmax",
    "venturus",
    "pier",
    "ecore",
    "luby",
    "wevy-cloud",
    "enjoei",
    "objective",
    "caf",
    "intera",
    "limaconsulting",
    "townsq",
    "atlantico",
    "housi",
    "ubots",
    "remessaonline",
    "exati",
    "lastlink",
    "aarin",
    "mereo",
    "celcoin",
    "pitang",
    "warren",
    "amigoz",
    "b3",
    "aiko",
    "grupogopwr",
    "labwarebrasil",
    "kiwify",
    "unico",
    "superfrete",
    "v4company",
    "vitru",
    "trillia",
    "agtaxtech",
)


def _slugify(title: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (title or "").lower())
    ascii_txt = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", ascii_txt).strip("-")


def discover_inhire_tenants_from_urls(urls: Iterable[str]) -> list[str]:
    """Extract unique valid <tenant>.inhire.app subdomains from arbitrary job URLs."""
    tenants: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        host = (urlparse((raw or "").strip()).netloc or "").lower().split(":")[0]
        if not host.endswith(".inhire.app"):
            continue
        tenant = host[: -len(".inhire.app")]
        if not tenant or "." in tenant or tenant in _RESERVED_SUBDOMAINS or tenant in seen:
            continue
        seen.add(tenant)
        tenants.append(tenant)
    return tenants


class InHireScraper(JobSource):
    """Public API scraper for InHire career pages (api.inhire.app/job-posts/public/pages)."""

    name = "inhire"

    def __init__(
        self,
        tenants: list[str] | None = None,
        *,
        remote_only: bool = True,
        dev_only: bool = True,
        max_workers: int = 12,
    ):
        if tenants is not None:
            raw_tenants = list(tenants)
        else:
            raw_tenants = [*env_list("INHIRE_TENANTS"), *DEFAULT_INHIRE_TENANTS]
        seen: set[str] = set()
        self._tenants: list[str] = []
        for t in raw_tenants:
            clean = (t or "").strip().lower()
            if clean and clean not in seen:
                seen.add(clean)
                self._tenants.append(clean)
        self._remote_only = remote_only
        self._dev_only = dev_only
        self._max_workers = max(1, max_workers)

    def _fetch_tenant_pages(self, tenant: str, timeout: int = 10) -> dict | None:
        req = urllib.request.Request(
            f"{INHIRE_API_BASE}/job-posts/public/pages",
            headers={
                "X-Tenant": tenant,
                "Accept": "application/json",
                "User-Agent": _UA,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("inhire tenant fetch failed tenant=%s err=%s", tenant, exc)
            return None

    def _fetch_job_detail(self, tenant: str, job_id: str, timeout: int = 12) -> tuple[int, dict | None]:
        req = urllib.request.Request(
            f"{INHIRE_API_BASE}/job-posts/public/pages/{job_id}",
            headers={
                "X-Tenant": tenant,
                "Accept": "application/json",
                "User-Agent": _UA,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except Exception as exc:  # noqa: BLE001
            logger.debug("inhire detail fetch failed %s/%s: %s", tenant, job_id, exc)
            return 0, None

    def _parse_tenant_pages(self, tenant: str, payload: dict) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        company = (payload.get("tenantName") or tenant).strip()
        items = payload.get("jobsPage") or []
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            status = (item.get("status") or "").strip().lower()
            if status and status != "published":
                continue
            title = (item.get("displayName") or "").strip()
            job_id = str(item.get("jobId") or "").strip()
            if not title or not job_id:
                continue
            if self._dev_only and not _is_dev_category(title):
                continue

            workplace = (item.get("workplaceType") or "").strip()
            location = (item.get("location") or "").strip() or "Brasil"
            is_remote = workplace.lower() == "remote" or bool(_REMOTE_TEXT_RE.search(f"{title} {location}"))
            if self._remote_only and not is_remote:
                continue

            career_page_id = (item.get("careerPageId") or "").strip()
            prefix = f"/{career_page_id}" if career_page_id and career_page_id != "default" else ""
            slug = _slugify(title)
            job_url = (
                f"https://{tenant}.inhire.app{prefix}/vagas/{job_id}/{slug}"
                if slug
                else f"https://{tenant}.inhire.app{prefix}/vagas/{job_id}"
            )

            out.append({
                "id": f"ih:{tenant}:{job_id}",
                "source": self.name,
                "url": job_url,
                "apply_url": job_url,
                "title": title,
                "company": company,
                "location": location,
                "modality": workplace or ("Remote" if is_remote else ""),
                "seniority": "",
                "remote": is_remote,
                "salary": "",
                "posted_at": None,
                "body": "",
            })
        return out

    def scrape(self) -> list[dict]:
        if not self._tenants:
            return []

        def _worker(t: str) -> list[dict]:
            payload = self._fetch_tenant_pages(t)
            return self._parse_tenant_pages(t, payload) if payload else []

        jobs: list[dict] = []
        seen: set[str] = set()
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            for batch in pool.map(_worker, self._tenants):
                for job in batch:
                    if job["id"] not in seen:
                        seen.add(job["id"])
                        jobs.append(job)
        return jobs

    def fetch_detail(self, url: str) -> dict:
        parsed = parse_inhire_url(url)
        if not parsed:
            return {}
        tenant, job_id = parsed
        status_code, payload = self._fetch_job_detail(tenant, job_id)
        if status_code == 404:
            return {"expired": True, "body": ""}
        if not isinstance(payload, dict):
            return {}
        job_status = (payload.get("status") or "").strip().lower()
        if job_status and job_status != "published":
            return {"expired": True, "body": ""}

        parts: list[str] = []
        if payload.get("tenantName"):
            parts.append(f"Empresa: {payload['tenantName']}")
        if payload.get("location"):
            parts.append(f"Local: {payload['location']}")
        if payload.get("workplaceType"):
            parts.append(f"Modalidade: {payload['workplaceType']}")
        if payload.get("contractType"):
            ctypes = ", ".join(str(c) for c in payload["contractType"] if c)
            if ctypes:
                parts.append(f"Contrato: {ctypes}")
        desc = _html_to_text(payload.get("description") or "")
        if desc:
            parts.append(desc)

        return {
            "expired": False,
            "body": "\n\n".join(parts)[:8000],
            "apply_url": url,
        }
