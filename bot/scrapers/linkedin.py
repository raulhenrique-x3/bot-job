import logging
import re
import time

from playwright.sync_api import TimeoutError as PWTimeout

from bot.config import env_int
from bot.scrapers.base import JobSource

logger = logging.getLogger("jobbot.scrapers.linkedin")

BASE = "https://www.linkedin.com"


def parse_title_from_text(li) -> str:
    """Fallback for logged-in cards with no strong/title selector."""
    first = li.get_text("\n", strip=True).split("\n")
    return first[0].strip() if first else ""


class LinkedInScraper(JobSource):
    name = "linkedin"

    def __init__(self, browser, search_terms: list[str], max_days: int = 10, remote_only: bool = False):
        self._browser = browser
        self._terms = [t.strip() for t in search_terms if t.strip()] or ["desenvolvedor"]
        self._max_days = max_days
        self._remote_only = remote_only

    def scrape(self) -> list[dict]:
        jobs: list[dict] = []
        max_pages = env_int("MAX_PAGES_PER_PORTAL", 2)
        seen: set[str] = set()
        for term in self._terms:
            for page_n in range(1, max_pages + 1):
                url = self._search_url(term, page_n)
                html = self._page_html(url)
                if not html:
                    break
                found = self._parse_listing(html)
                new = [j for j in found if j["id"] not in seen]
                if not new:
                    break
                seen.update(j["id"] for j in new)
                jobs.extend(new)
                time.sleep(2.5)
        return jobs

    def _search_url(self, term: str, page_n: int = 1) -> str:
        days = min(self._max_days, 14)
        start = (page_n - 1) * 25
        f_tpr = f"f_TPR=r{days * 86400}"
        remote = "f_WT=2&" if self._remote_only else ""
        return (
            f"{BASE}/jobs/search/?keywords={term.replace(' ', '%20')}"
            f"&location=Brasil&{remote}{f_tpr}&start={start}&origin=JOB_SEARCH_PAGE_QUERY"
        )

    def _page_html(self, url: str) -> str | None:
        from bot.scrapers.browser import goto

        try:
            with goto(self._browser, url) as page:
                if page is None:
                    return None
                if "/authwall" in page.url or "/login" in page.url or "/checkpoint" in page.url:
                    logger.warning("LinkedIn session lost — re-run login script")
                    return None
                page.wait_for_selector("a[href*='/jobs/view/']", timeout=8000)
                return page.content()
        except PWTimeout:
            logger.warning("LinkedIn search returned no job cards for %s", url)
            return None

    def _parse_listing(self, html: str) -> list[dict]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        out: dict[str, dict] = {}

        # Layout A: guest cards (base-search-card), layout B: logged-in (li[data-occludable-job-id])
        for li in soup.select("li[data-occludable-job-id]"):
            jid = li.get("data-occludable-job-id", "")
            if not jid:
                continue
            title_el = li.select_one(".job-card-container__link strong") or li.select_one(
                ".artdeco-entity-lockup__title .job-card-container__link"
            )
            title = title_el.get_text(strip=True) if title_el else parse_title_from_text(li)
            company_el = li.select_one(".artdeco-entity-lockup__subtitle")
            company = company_el.get_text(" ", strip=True) if company_el else ""
            text = li.get_text("\n", strip=True)
            body = ""  # full text of logged-in view
            location = ""
            remote = False
            for ln in text.split("\n"):
                if any(k in ln for k in ("Remoto", "Híbrido", "Presencial", "Brazil", "Brasil")) and len(ln) < 80:
                    location = ln.strip()
                    remote = "Remoto" in ln or "Remote" in ln.lower()
                    break
            out[f"li:{jid}"] = {
                "source": self.name,
                "id": f"li:{jid}",
                "url": f"{BASE}/jobs/view/{jid}/",
                "title": title,
                "company": company,
                "location": location,
                "remote": remote,
                "salary": "",
                "posted_at": "",
                "body": body,
                "guest_html": False,
            }

        for card in soup.select("a.base-search-card__link, a[href*='/jobs/view/'].base-card"):
            href = card.get("href", "")
            match = re.search(r"/jobs/view/(\d+)", href)
            if not match:
                continue
            job_id = f"li:{match.group(1)}"
            if job_id in out:
                continue
            wrapper = card.find_parent("li") or card
            title_el = wrapper.select_one(".base-search-card__title")
            company_el = wrapper.select_one(".base-search-card__subtitle")
            time_el = wrapper.select_one("time[datetime]")
            meta_el = wrapper.select_one(".job-search-card__location")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            location = meta_el.get_text(strip=True) if meta_el else ""
            out[job_id] = {
                "source": self.name,
                "id": job_id,
                "url": f"{BASE}/jobs/view/{match.group(1)}/",
                "title": title,
                "company": company,
                "location": location,
                "remote": bool(re.search(r"remoto|remote", location, re.I)),
                "salary": "",
                "posted_at": time_el.get("datetime", "") if time_el else "",
                "body": "",
                "guest_html": True,
            }
        return list(out.values())

    def fetch_detail(self, url: str) -> dict:
        from bot.scrapers.browser import goto

        detail = {"body": "", "expired": False}
        try:
            with goto(self._browser, url) as page:
                if page is None:
                    return detail
                if "/authwall" in page.url or "/login" in page.url:
                    logger.warning("LinkedIn details blocked for %s", url)
                    return detail
                try:
                    page.wait_for_selector(".show-more-less-html, .jobs-description", timeout=8000)
                except PWTimeout:
                    pass
                detail["body"] = page.inner_text("body")[:8000]
        except PWTimeout:
            pass
        return detail
