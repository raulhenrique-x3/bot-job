import logging
import random
import re
import time

from bot.config import env_int
from bot.scrapers.base import JobSource

logger = logging.getLogger("jobbot.scrapers.programathor")

BASE = "https://programathor.com.br"
EXPIRED_RE = re.compile(r"VENCIDA|ENCERRADA", re.IGNORECASE)
REMOTE_RE = re.compile(r"REMOTO|HOME OFFICE", re.IGNORECASE)


class ProgramathorScraper(JobSource):
    name = "programathor"

    def __init__(self, browser):
        self._browser = browser

    def scrape(self) -> list[dict]:
        jobs: list[dict] = []
        max_pages = env_int("MAX_PAGES_PER_PORTAL", 2)
        for page in range(1, max_pages + 1):
            html = self._page_html(f"{BASE}/jobs?page={page}")
            if not html:
                break
            found = self._parse_listing(html)
            new = [j for j in found if j["id"] not in {x["id"] for x in jobs}]
            if not new:
                break
            jobs.extend(new)
            time.sleep(random.uniform(1.5, 3.0))
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
        for card in soup.select(".cell-list:has(a[href^='/jobs/'])"):
            anchor = card.select_one("a[href^='/jobs/']")
            if not anchor:
                continue
            href = anchor.get("href", "")
            match = re.match(r"^/jobs/(\d+)", href)
            if not match:
                continue
            job_id = match.group(1)
            title_el = card.select_one("h3")
            if not title_el:
                continue
            icons = card.select(".cell-list-content-icon span")
            company = location = seniority = contract = ""
            for span in icons:
                text = span.get_text(" ", strip=True)
                icon = span.select_one("i")
                cls = icon.get("class", []) if icon else []
                if "fa-briefcase" in cls:
                    company = text
                elif "fa-map-marker-alt" in cls:
                    location = text
                elif "far fa-chart-bar" in " ".join(cls):
                    seniority = text
                elif "fa-building" in cls:
                    contract = text
            tags = [t.get_text(strip=True) for t in card.select(".tag-list")]
            body = "Título: {}\nEmpresa: {}\nLocal: {}\nContratação: {}\nSenioridade: {}\nStack: {}".format(
                title_el.get_text(strip=True), company or "—", location or "—",
                contract or "—", seniority or "—", ", ".join(tags) or "—",
            )
            out.append(
                {
                    "source": self.name,
                    "id": job_id,
                    "url": f"{BASE}{href}",
                    "title": title_el.get_text(strip=True),
                    "company": company,
                    "location": location,
                    "seniority": seniority,
                    "remote": bool(REMOTE_RE.search(card.get_text(" ", strip=True))),
                    "salary": "",
                    "posted_at": None,
                    "body": body,
                }
            )
        return out


    def fetch_detail(self, url: str) -> dict:
        from bot.scrapers.browser import goto

        detail = {"body": "", "expired": False}
        with goto(self._browser, url) as page:
            if page is None:
                return detail
            text = page.inner_text("body")
            detail["expired"] = bool(EXPIRED_RE.search(text[:2000]))
            detail["body"] = text[:8000]
        return detail
