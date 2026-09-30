import json
import logging
import re
import time

from bot.config import env_int
from bot.scrapers.base import JobSource

logger = logging.getLogger("jobbot.scrapers.geekhunter")

BASE = "https://www.geekhunter.com"


class GeekHunterScraper(JobSource):
    name = "geekhunter"

    def __init__(self, browser, search_terms: list[str] | None = None):
        self._browser = browser
        self._terms = [t.strip() for t in (search_terms or ["python", "node", "react"]) if t.strip()]

    def scrape(self) -> list[dict]:
        jobs: list[dict] = []
        max_pages = env_int("MAX_PAGES_PER_PORTAL", 2)
        seen: set[str] = set()
        for term in self._terms:
            for page_n in range(1, max_pages + 1):
                qs = f"?searchTerm={term}&page={page_n}" if page_n > 1 else f"?searchTerm={term}"
                html = self._page_html(f"{BASE}/pt/vagas{qs}")
                if not html:
                    break
                found = self._parse_listing(html)
                new = [j for j in found if j["id"] not in seen]
                if not new:
                    break
                seen.update(j["id"] for j in new)
                jobs.extend(new)
                time.sleep(2.0)
        return jobs

    def _page_html(self, url: str) -> str | None:
        from bot.scrapers.browser import goto

        with goto(self._browser, url) as page:
            if page is None:
                return None
            return page.content()

    def _parse_listing(self, html: str) -> list[dict]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        out = []
        for li in soup.select("li[id^='job-']"):
            anchor = li.select_one("h3 a[href*='/jobs/']")
            if not anchor:
                continue
            href = anchor.get("href", "")
            path = href.replace(f"{BASE}", "") if href.startswith("http") else href
            parts = [p for p in path.split("/") if p]
            # /pt/{company-slug}/jobs/{job-slug}
            if len(parts) < 4:
                continue
            company_slug, job_slug = parts[1], parts[-1]
            job_id = "gh:" + job_slug
            lines = [
                s.strip()
                for s in li.get_text("\n", strip=True).split("\n")
                if s.strip()
            ]
            title = anchor.get_text(strip=True)
            seniority = ""
            modality = ""
            location = ""
            desc = ""
            for idx, ln in enumerate(lines):
                if idx > 0 and ln.upper() == ln and len(ln) > 3 and seniority == "":
                    seniority = ln.title()
                elif idx > 0 and ln.upper() in ("HÍBRIDO", "REMOTO", "PRESENCIAL"):
                    modality = ln.title()
                elif idx > 0 and not location and ("," in ln or "Brasil" in ln):
                    location = ln
            desc = ""
            for p in li.select("p"):
                text = p.get_text(" ", strip=True)
                if len(text) > 120 and ("vaga" in text.lower() or "busca" in text.lower()):
                    desc = text
                    break
            body = "Título: {}\nSenioridade: {}\nModalidade: {}\nLocal: {}\nResumo: {}".format(
                title, seniority or "—", modality or "—", location or "—", desc or "—"
            )
            company = company_slug.replace("-", " ").title()
            company = re.sub(r"\s*\d+$", "", company)
            out.append(
                {
                    "source": self.name,
                    "id": job_id,
                    "url": anchor.get("href") or f"{BASE}{path}",
                    "title": title,
                    "company": company,
                    "location": location,
                    "modality": modality,
                    "seniority": seniority,
                    "remote": "remoto" in modality.lower(),
                    "salary": "",
                    "posted_at": self._posted_from_card(li),
                    "body": body,
                }
            )
        return out

    @staticmethod
    def _posted_from_card(li) -> str | None:
        text = li.get_text(" ", strip=True)
        import re as _re

        from datetime import datetime, timedelta, timezone

        m = _re.search(r"(?:atualizada|publicada)[^0-9]*(\d+)\s*(dia|semana|mês|mes)", text, _re.IGNORECASE)
        if not m:
            return None
        n, unit = int(m.group(1)), m.group(2).lower()
        days = n * 7 if "semana" in unit else n * 30 if "mês" in unit or "mes" in unit else n
        return (datetime.now(timezone.utc) - timedelta(days=n if days is None else days)).isoformat()

    def fetch_detail(self, url: str) -> dict:
        from bot.scrapers.browser import goto

        detail = {"body": "", "expired": False}
        with goto(self._browser, url, timeout_ms=30000) as page:
            if page is None:
                return detail
            text = page.inner_text("body")
            detail["body"] = text[:8000]
            head = text[:3000].lower()
            detail["expired"] = "encerrada" in head or "não aceita" in head
            # enrich structured fields shown only on detail page
            for chunk in ("Faixa de Remuneração", "Nível de Experiência"):
                i = text.find(chunk)
                if i >= 0:
                    detail["body"] += "\n" + text[i : i + 400]
        return detail
