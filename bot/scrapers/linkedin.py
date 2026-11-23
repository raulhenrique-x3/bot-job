import logging
import re
import time
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from playwright.sync_api import TimeoutError as PWTimeout

from bot.appliers.detector import detect_platform
from bot.config import env_int
from bot.scrapers.base import JobSource

logger = logging.getLogger("jobbot.scrapers.linkedin")

BASE = "https://www.linkedin.com"
_CLOSED_JOB_RE = re.compile(
    r"n[ãa]o aceita mais candidaturas|no longer accepting applications",
    re.IGNORECASE,
)


def build_profile_search_terms(profile, max_terms: int = 4) -> list[str]:
    """Derive focused LinkedIn search queries from the candidate's verified profile."""
    if profile is None:
        return ["desenvolvedor"]

    headline = (getattr(profile, "headline", "") or "").lower()
    skills_list = [str(s).lower() for s in (getattr(profile, "skills", None) or [])]
    blob = f"{headline} {' '.join(skills_list)}"

    has = lambda *kws: any(k in blob for k in kws)
    terms: list[str] = []

    if has("fullstack", "full stack", "full-stack") and has("react", "next.js") and has("node", "typescript"):
        terms.append("Fullstack React Node")
    elif has("fullstack", "full stack", "full-stack") and has("python"):
        terms.append("Fullstack Python")

    if has("python") and has("fastapi", "flask", "django", "backend", "back-end", "fullstack", "full stack"):
        terms.append("Desenvolvedor Python")

    if has("react", "next.js") and has("typescript", "front-end", "frontend"):
        terms.append("Frontend React TypeScript")

    if has("llm", "genai", "ia", "ai ", "inteligência artificial", "machine learning", "langchain", "rag"):
        terms.append("AI Engineer Python")

    if len(terms) < max_terms and has("node.js", "nestjs", "express"):
        terms.append("Desenvolvedor Node TypeScript")

    if len(terms) < max_terms and has(".net", "c#"):
        terms.append("Desenvolvedor .NET")

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in terms:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique[:max_terms] or ["desenvolvedor"]


def parse_title_from_text(li) -> str:
    """Fallback for logged-in cards with no strong/title selector."""
    first = li.get_text("\n", strip=True).split("\n")
    return first[0].strip() if first else ""


class LinkedInScraper(JobSource):
    name = "linkedin"

    def __init__(
        self,
        browser,
        search_terms: list[str],
        max_days: int = 10,
        remote_only: bool = True,
        easy_apply_only: bool = True,
        location: str = "Brasil",
        include_external: bool = False,
    ):
        self._browser = browser
        self._terms = [t.strip() for t in search_terms if t.strip()] or ["desenvolvedor"]
        self._max_days = max_days
        self._remote_only = remote_only
        self._easy_apply_only = easy_apply_only
        self._location = (location or "Brasil").strip()
        self._include_external = include_external
        self.session_lost = False

    def scrape(self) -> list[dict]:
        jobs: list[dict] = []
        max_pages = env_int("MAX_PAGES_PER_PORTAL", 2)
        seen: set[str] = set()
        passes: list[tuple[bool, int]] = [(self._easy_apply_only, max_pages)]
        if self._easy_apply_only and self._include_external:
            # Also collect external ATS listings (e.g. *.inhire.app, Greenhouse, Lever, Ashby)
            passes.append((False, 1))
        for easy_apply, pages_limit in passes:
            for term in self._terms:
                for page_n in range(1, pages_limit + 1):
                    url = self._search_url(term, page_n, easy_apply=easy_apply)
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

    def _search_url(self, term: str, page_n: int = 1, *, easy_apply: bool | None = None) -> str:
        days = min(self._max_days, 14)
        start = (page_n - 1) * 25
        params = [
            f"keywords={quote_plus(term)}",
            f"location={quote_plus(self._location)}",
            f"f_TPR=r{days * 86400}",
        ]
        if self._remote_only:
            params.append("f_WT=2")
        use_easy = self._easy_apply_only if easy_apply is None else easy_apply
        if use_easy:
            params.append("f_AL=true")
        params.append(f"start={start}")
        params.append("origin=JOB_SEARCH_PAGE_QUERY")
        return f"{BASE}/jobs/search/?{'&'.join(params)}"

    def _page_html(self, url: str) -> str | None:
        from bot.scrapers.browser import goto

        try:
            with goto(self._browser, url, timeout_ms=30000, block_heavy_assets=True) as page:
                if page is None:
                    return None
                if "/authwall" in page.url or "/login" in page.url or "/checkpoint" in page.url:
                    self.session_lost = True
                    logger.warning("LinkedIn session lost — re-run login script")
                    return None
                page.wait_for_selector(
                    "a[href*='/jobs/view/'], [componentkey^='job-card-component-ref-'], li[data-occludable-job-id]",
                    timeout=10000,
                )
                return page.content()
        except PWTimeout:
            logger.warning("LinkedIn search returned no job cards for %s", url)
            return None

    def _parse_listing(self, html: str) -> list[dict]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        out: dict[str, dict] = {}

        # Layout 0: 2026 LinkedIn SDUI cards ([componentkey^="job-card-component-ref-"])
        for card in soup.select("[componentkey^='job-card-component-ref-']"):
            ckey = card.get("componentkey", "")
            m = re.search(r"job-card-component-ref-(\d+)", ckey)
            if not m:
                continue
            jid = m.group(1)
            job_id = f"li:{jid}"
            if job_id in out:
                continue
            lines = [ln.strip() for ln in card.get_text("\n", strip=True).split("\n") if ln.strip()]
            # Filter accessibility prefixes like "Selecionada, ..." or "(vaga verificada)" / "(Verified job)"
            clean_lines = [
                ln for ln in lines
                if not re.match(
                    r"^(selecionada,|selected,|vaga verificada|\(vaga verificada\)|verified job|\(verified job\))$",
                    ln,
                    re.I,
                )
            ]
            title = clean_lines[0] if clean_lines else ""
            title = re.sub(r"^(?:selecionada|selected),\s*", "", title, flags=re.I)
            title = re.sub(r"\s*\((?:vaga verificada|verified job)\)\s*$", "", title, flags=re.I).strip()
            company = ""
            for ln in clean_lines[1:]:
                cand = re.sub(r"\s*\((?:vaga verificada|verified job)\)\s*$", "", ln, flags=re.I).strip()
                if cand.lower() == title.lower():
                    continue
                company = cand
                break
            location = ""
            remote = False
            for ln in clean_lines[1:]:
                if ln.lower() == title.lower() or ln.lower() == company.lower():
                    continue
                if any(k in ln for k in ("Remoto", "Remote", "Híbrido", "Hybrid", "Presencial", "On-site", "Brasil", "Brazil")) and len(ln) < 90:
                    location = ln
                    remote = bool(re.search(r"remot", ln, re.I))
                    break
            out[job_id] = {
                "source": self.name,
                "id": job_id,
                "url": f"{BASE}/jobs/view/{jid}/",
                "title": title,
                "company": company,
                "location": location,
                "remote": remote,
                "salary": "",
                "posted_at": "",
                "body": "",
                "guest_html": False,
            }

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
            with goto(self._browser, url, timeout_ms=30000, block_heavy_assets=True) as page:
                if page is None:
                    return detail
                if "/authwall" in page.url or "/login" in page.url:
                    logger.warning("LinkedIn details blocked for %s", url)
                    return detail
                try:
                    page.wait_for_selector(".show-more-less-html, .jobs-description", timeout=8000)
                except PWTimeout:
                    pass
                body = page.inner_text("body")[:8000]
                detail["expired"] = bool(_CLOSED_JOB_RE.search(body[:3000]))
                detail["body"] = body
                ext_url = self._extract_external_apply_url(page)
                if ext_url:
                    detail["apply_url"] = ext_url
        except PWTimeout:
            pass
        return detail

    @staticmethod
    def _extract_external_apply_url(page) -> str:
        """Extract direct external ATS URL (e.g. InHire/Greenhouse/Lever) from LinkedIn /safety/go/ CTA."""
        try:
            anchors = page.locator("a[href*='/safety/go/']")
            for i in range(min(anchors.count(), 15)):
                el = anchors.nth(i)
                href = el.get_attribute("href") or ""
                qs = parse_qs(urlparse(href).query)
                if not qs.get("url"):
                    continue
                target = unquote(qs["url"][0]).strip()
                if not target or "linkedin.com" in target.lower():
                    continue
                plat = detect_platform(target)
                if plat not in ("generic", "linkedin"):
                    return target
                txt = (el.inner_text() or "").strip().lower()
                aria = (el.get_attribute("aria-label") or "").strip().lower()
                if any(k in f"{txt} {aria}" for k in ("candidatar", "apply")):
                    return target
        except Exception:  # noqa: BLE001
            pass
        return ""
