import json
import logging
import time
import urllib.request

from bot.config import env_int
from bot.scrapers.base import JobSource

logger = logging.getLogger("jobbot.scrapers.querovagas")

BASE = "https://www.querovagastech.com.br"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class QueroVagasScraper(JobSource):
    name = "querovagas"

    def __init__(self, browser, work_mode: str = "Remote"):
        self._work_mode = work_mode
        self._detail_api_by_url: dict[str, str] = {}

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

    def _listing_url(self, page: int, page_size: int) -> str:
        qs = [
            f"page={page}",
            f"pageSize={page_size}",
            f"sort=postedAt%3Adesc",
        ]
        if self._work_mode:
            qs.append(f"workMode={self._work_mode}")
        return f"{BASE}/api/jobs?{'&'.join(qs)}"

    def scrape(self) -> list[dict]:
        jobs: list[dict] = []
        max_pages = env_int("MAX_PAGES_PER_PORTAL", 2)
        page_size = 20
        self._detail_api_by_url.clear()
        for page_n in range(1, max_pages + 1):
            payload = self._get_json(self._listing_url(page_n, page_size))
            if not payload:
                break
            items = payload.get("items") or []
            if not items:
                break
            jobs.extend(self._parse_items(items))
            total = int(payload.get("total") or 0)
            if page_n * page_size >= total:
                break
            time.sleep(1.0)
        return jobs

    def _parse_items(self, items: list) -> list[dict]:
        out = []
        for item in items:
            if not item.get("title") or not item.get("id"):
                continue
            uid = str(item["id"])
            apply_url = item.get("applyUrl") or f"{BASE}/"
            self._detail_api_by_url[apply_url] = f"{BASE}/api/jobs/{uid}"
            out.append(
                {
                    "source": self.name,
                    "id": "qv:" + uid,
                    "url": apply_url,
                    "title": (item.get("title") or "").strip(),
                    "company": (item.get("company") or "").strip(),
                    "location": (item.get("location") or "").strip(),
                    "modality": (item.get("workMode") or "").strip(),
                    "seniority": (item.get("seniority") or "").strip(),
                    "remote": (item.get("workMode") or "").lower() == "remote",
                    "salary": "",
                    "posted_at": item.get("postedAt"),
                    "body": self._body_from_item(item),
                }
            )
        return out

    @staticmethod
    def _body_from_item(item: dict) -> str:
        parts = [
            "Título: {}".format(item.get("title") or "—"),
            "Empresa: {}".format(item.get("company") or "—"),
            "Local: {}".format(item.get("location") or "—"),
            "Modalidade: {}".format(item.get("workMode") or "—"),
            "Senioridade: {}".format(item.get("seniority") or "—"),
            "Contrato: {}".format(item.get("employmentType") or "—"),
        ]
        return "\n".join(parts)

    def fetch_detail(self, url: str) -> dict:
        detail = {"body": "", "expired": False}
        api_url = self._detail_api_by_url.get(url)
        if not api_url:
            return detail
        payload = self._get_json(api_url)
        if not payload:
            return detail
        description = (payload.get("description") or "").strip()
        detail["body"] = description[:8000]
        head = description[:3000].lower()
        detail["expired"] = not description
        return detail
