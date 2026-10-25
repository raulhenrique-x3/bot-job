"""DevAgas (devagas-liard.vercel.app) — agregador com API pública GET /api/vagas.

Resposta:  {"jobs": [...], "stats": {"lastCrawlTime": str, "totalJobsCrawled": int}}
Cada job:  id, title, company, location, sourceUrl, description, requirements,
           benefits, salary, postedAt (ISO), skills, suggestedPost, applyUrl,
           contactEmail, contactName.
"""

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from bot.config import env, env_bool, env_int
from bot.scrapers.base import JobSource

logger = logging.getLogger("jobbot.scrapers.devagas")

BASE = "https://devagas-liard.vercel.app"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class DevagasScraper(JobSource):
    name = "devagas"

    def __init__(self, browser=None):
        self._browser = browser

    def _api_url(self) -> str:
        base = env("DEVAGAS_URL", BASE).rstrip("/")
        max_pages = env_int("MAX_PAGES_PER_PORTAL", 2)
        page_size = env_int("DEVAGAS_PAGE_SIZE", 100)
        params = {
            "page": 1,
            "pageSize": max_pages * page_size,
            "sort": "postedAt:desc",
        }
        return f"{base}/api/vagas?{urllib.parse.urlencode(params)}"

    def _get_json(self, url: str, timeout: int = 30) -> dict | None:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Accept-Language": "pt-BR,pt;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("api fail url=%s err=%s", url, exc)
            return None

    @staticmethod
    def _body_from_item(item: dict) -> str:
        parts = [
            "Título: {}".format(item.get("title") or "—"),
            "Empresa: {}".format(item.get("company") or "—"),
            "Local: {}".format(item.get("location") or "—"),
            "Salário: {}".format(item.get("salary") or "—"),
        ]
        description = (item.get("description") or "").strip()
        if description:
            parts.append(description)

        for label, field in (("Requisitos", "requirements"), ("Benefícios", "benefits"), ("Skills", "skills")):
            values = [str(v).strip() for v in (item.get(field) or []) if str(v).strip()]
            if values:
                parts.append("{}: {}".format(label, "; ".join(values)))

        email = (item.get("contactEmail") or "").strip()
        if email:
            parts.append("Contato: {}".format(email))
        return "\n".join(parts)

    def _parse_items(self, items: list) -> list[dict]:
        out = []
        for item in items:
            if not item.get("title"):
                continue
            uid = str(item.get("id") or item.get("sourceUrl") or item.get("applyUrl") or "")
            if not uid:
                continue
            url = (
                item.get("applyUrl")
                or item.get("sourceUrl")
                or ""
            ).strip()
            if not url:
                continue
            remote = "remot" in (item.get("location") or "").lower()
            posted = item.get("postedAt") or ""
            if posted:
                try:
                    datetime.fromisoformat(posted.replace("Z", "+00:00"))
                except ValueError:
                    posted = ""
            out.append(
                {
                    "source": self.name,
                    "id": "dv:" + uid,
                    "url": url,
                    "title": (item.get("title") or "").strip(),
                    "company": (item.get("company") or "").strip() or "Confidencial",
                    "location": (item.get("location") or "").strip(),
                    "modality": "",
                    "seniority": "",
                    "remote": remote,
                    "salary": (item.get("salary") or "").strip(),
                    "posted_at": posted,
                    "body": self._body_from_item(item),
                }
            )
        return out

    def scrape(self) -> list[dict]:
        if not env_bool("DEVAGAS_ENABLED", True):
            return []
        payload = self._get_json(self._api_url())
        if not payload:
            return []
        items = payload.get("jobs") or []
        if not items:
            return []
        jobs = self._parse_items(items)
        logger.info("devagas items=%d parsed=%d", len(items), len(jobs))
        return jobs

    def fetch_detail(self, url: str) -> dict:
        # listing payload já traz descrição; nada a buscar no detalhe
        return {"body": "", "expired": False}
