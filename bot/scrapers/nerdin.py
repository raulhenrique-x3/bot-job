import logging
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin

from bot.config import env_int
from bot.scrapers.base import JobSource

logger = logging.getLogger("jobbot.scrapers.nerdin")

BASE = "https://www.nerdin.com.br"
LISTING = BASE + "/vagas-desenvolvedor-sistemas.php"
EXPIRED_RE = re.compile(r"VENCIDA|ENCERRADA|EXPIRADA", re.IGNORECASE)


class NerdinScraper(JobSource):
    name = "nerdin"

    def __init__(self, browser, path: str = "/vagas-desenvolvedor-sistemas.php",
                 query: str = "filtro_home_office=1"):
        self._browser = browser
        self._path = path
        self._query = query

    def scrape(self) -> list[dict]:
        jobs: list[dict] = []
        max_pages = env_int("MAX_PAGES_PER_PORTAL", 2)
        for page in range(1, max_pages + 1):
            qs = self._query
            if page > 1:
                qs = qs + f"&pagina={page}" if self._query else f"pagina={page}"
            html = self._page_html(f"{LISTING}?{qs}")
            if not html:
                break
            found = self._parse_listing(html)
            new = [j for j in found if j["id"] not in {x["id"] for x in jobs}]
            if not new:
                break
            jobs.extend(new)
            if page * 20 >= self._total_results(html):
                break
            time.sleep(random.uniform(1.5, 3.0))
        return jobs

    def _page_html(self, url: str) -> str | None:
        from bot.scrapers.browser import goto

        with goto(self._browser, url) as page:
            if page is None:
                return None
            return page.content()

    @staticmethod
    def _total_results(html: str) -> int:
        m = re.search(r"de\s+([\d.]+)\s*vag", html)
        return int(m.group(1).replace(".", "")) if m else 0

    def _parse_listing(self, html: str) -> list[dict]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        out = []
        for card in soup.select("div.vaga-card[data-href]"):
            href = card.get("data-href") or ""
            if not href:
                continue
            anchor = card.select_one("a.btn-ver-vaga[href]")
            if anchor and anchor.get("href"):
                href = anchor.get("href")
            url = f"{BASE}/{href.lstrip('/')}" if href.startswith("/") else href
            if not url.startswith("http"):
                url = f"{BASE}/{url}"
            m = re.search(r"-(\d+)\.php$", url)
            job_id = "nd:" + (m.group(1) if m else href)

            title_el = card.select_one("h3.vaga-titulo")
            if not title_el:
                continue
            for badge in title_el.select(".vaga-nova-badge"):
                badge.decompose()
            title = title_el.get_text(" ", strip=True).strip()

            contract = seniority = modality = ""
            resumo = card.select_one("p.vaga-resumo-linha")
            if resumo:
                parts = [p.strip() for p in resumo.get_text(" ", strip=True).split("•")]
                if len(parts) == 3:
                    contract, seniority, modality = parts
                elif len(parts) == 2:
                    a, b = parts
                    if a.lower() in ("clt", "pj", "freelancer"):
                        contract, seniority = a, b
                    else:
                        seniority, modality = a, b
                elif len(parts) == 1:
                    seniority = parts[0]
                if seniority and not modality and seniority.lower() in (
                    "home office", "híbrido", "hibrido", "presencial",
                ):
                    modality, seniority = seniority, ""

            salary = ""
            sal_el = card.select_one("div.vaga-salario-destaque span")
            if sal_el:
                salary = sal_el.get_text(" ", strip=True)

            company = ""
            comp_el = card.select_one("span.vaga-empresa-nome")
            if comp_el:
                company = comp_el.get_text(" ", strip=True)

            location = ""
            loc_el = card.select_one("div.vaga-local-linha span")
            if loc_el:
                location = loc_el.get_text(" ", strip=True)

            posted_at = None
            time_el = card.select_one("p.vaga-meta-extra time[datetime]")
            if time_el:
                posted_at = self._iso(time_el.get("datetime"))
            area = ""
            meta = card.select_one("p.vaga-meta-extra")
            if meta:
                area = meta.get_text(" ", strip=True).split("•")[0].strip()

            tags = " ".join(
                a.get_text(" ", strip=True).lstrip("#")
                for a in card.select("div.vaga-hashtags a.hashtag")
            )

            body = "Título: {}\nEmpresa: {}\nLocal: {}\nModalidade: {}\nContratação: {}\nSenioridade: {}\nÁrea: {}\nSalário: {}\nStack: {}".format(
                title, company or "—", location or "—", modality or "—",
                contract or "—", seniority or "—", area or "—",
                salary or "—", tags or "—",
            )
            out.append(
                {
                    "source": self.name,
                    "id": job_id,
                    "url": unquote(url),
                    "title": title,
                    "company": company,
                    "location": location,
                    "modality": modality,
                    "seniority": seniority,
                    "remote": "home office" in (modality or location).lower() or "remoto" in (modality or location).lower(),
                    "salary": salary,
                    "posted_at": posted_at,
                    "body": body,
                }
            )
        return out

    @staticmethod
    def _iso(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
        except ValueError:
            return None

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
