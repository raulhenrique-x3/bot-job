"""LinkedIn feed post scraper for developer hiring posts (emails, external links, inbox)."""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PWTimeout

from bot.appliers.detector import detect_platform
from bot.config import env_list
from bot.scrapers.base import JobSource

logger = logging.getLogger("jobbot.scrapers.linkedin_posts")

DEFAULT_POST_QUERIES: tuple[str, ...] = (
    '"vaga" ("front-end" OR "frontend" OR "fullstack" OR "full stack") ("remoto" OR "e-mail" OR "currículo")',
    '"vaga" ("dev IA" OR "AI Engineer" OR "engenheiro de IA" OR "LLM") ("remoto" OR "currículo")',
    '("forward deployed engineer" OR "forwarded deployment" OR "founding engineer" OR "AI engineer") ("hiring" OR "remote" OR "apply" OR "email")',
    '("estamos contratando" OR "vaga aberta" OR "envie seu currículo") ("front-end" OR "fullstack" OR "engenheiro de software" OR "react" OR "python")',
)

# Roles specifically targeted for fast-tracking from LinkedIn posts / email jobs
TARGET_POST_ROLE_RE = re.compile(
    r"\b("
    r"front[- ]?end|"
    r"react(?:\.js|js)?|"
    r"next\.?js|"
    r"typescript|"
    r"full[- ]?stack|"
    r"forward(?:ed)?\s+deploy(?:ed|ment)(?:\s+engineer)?|"
    r"fde|"
    r"dev(?:eloper|envolvedor(?:a)?)?\s+(?:de\s+)?ia|"
    r"engenheir[oa]\s+(?:de\s+)?ia|"
    r"ai\s+(?:engineer|developer)|"
    r"machine\s+learning\s+engineer|"
    r"llm|"
    r"genai|"
    r"software\s+engineer|"
    r"engenheir[oa]\s+de\s+software|"
    r"product\s+engineer|"
    r"founding\s+engineer|"
    r"desenvolvedor(?:a)?\s+(?:de\s+software|python|node|web|react|full|front|pleno|s[êe]nior)"
    r")\b",
    re.IGNORECASE,
)

# Reject posts from candidates looking for work rather than employers/recruiters hiring
_JOB_SEEKER_RE = re.compile(
    r"(?:"
    r"em\s+busca\s+d[aeo]\s+(?:minha\s+)?(?:primeira\s+)?(?:nova\s+)?(?:oportunidade|recoloca[cç][aã]o|vaga)"
    r"|estou\s+aberto\s+a\s+(?:vagas|oportunidades)"
    r"|busco\s+(?:uma\s+)?(?:oportunidade|recoloca[cç][aã]o|vaga)(?:\s+como)?"
    r"|procuro\s+(?:uma\s+)?vaga"
    r"|open\s+to\s+work|#opentowork"
    r"|looking\s+for\s+(?:a\s+|my\s+)?(?:new\s+|next\s+)?(?:role|opportunity|job|position)"
    r"|estou\s+dispon[ií]vel\s+para\s+(?:novas\s+)?oportunidades"
    r"|meu\s+curr[ií]culo\s+(?:est[áa]|segue)"
    r"|buscando\s+recoloca[cç][aã]o"
    r")",
    re.IGNORECASE,
)

_HIRING_SIGNAL_RE = re.compile(
    r"(?:"
    r"\bvaga(?:s)?\s+(?:aberta|remota|100%\s*remota|para|de|home\s*office)"
    r"|\bestamos\s+(?:contratando|buscando|com\s+vaga)"
    r"|\boportunidade\s+(?:100%\s*remota|remota|para|de)\b"
    r"|\bcontrata[cç][aã]o\s+(?:clt|pj)\b"
    r"|(?<!n[ãa]o\s)\benvi(?:e|ar)\s+(?:o\s+|seu\s+)?(?:curr[ií]culo|cv)\b"
    r"|\bmand(?:e|ar)\s+(?:o\s+|seu\s+)?(?:curr[ií]culo|cv)\b"
    r"|\binteressados\s+(?:enviar|mandar|encaminhar|chamar|comentar|entrar)"
    r"|\bwe(?:'re|\s+are)\s+hiring\b"
    r"|\bhiring\s+(?:a|an|for)\b"
    r"|\blooking\s+for\s+(?:a|an)\s+(?:senior|mid|pleno|full[- ]?stack|front[- ]?end|ai|forward(?:ed)?|software|founding)\b"
    r"|\bsend\s+(?:your\s+)?(?:resume|cv)\s+(?:to|via)\b"
    r"|\bapply\s+(?:at|here|now|via)\b"
    r")",
    re.IGNORECASE,
)

_HIRING_EMAIL_CONTEXT_RE = re.compile(
    r"\b(vaga(?:s)?|oportunidade(?:s)?|hiring|contrata[cç][aã]o|sele[cç][aã]o|cv|curr[ií]culo|resume)\b",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js")
_PLACEHOLDER_LOCAL_PARTS = frozenset({"exemplo", "example", "seuemail", "your.email", "email", "nome", "user", "test", "teste"})
_PLACEHOLDER_DOMAINS = frozenset({"example.com", "example.org", "exemplo.com", "exemplo.com.br", "dominio.com", "domain.com"})

_SUBJECT_HINT_RE = re.compile(
    r"(?:assunto|subject)\s*(?:do\s+e-?mail|com(?:\s+o\s+t[ií]tulo)?)?\s*[:\-–]\s*[\"“']?([^\n\"”'|.]{3,80})",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"'()]+", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\b(remot[oa]?|home\s*office|anywhere|worldwide|teletrabalho)\b", re.IGNORECASE)


def is_target_role_post(text: str) -> bool:
    """Return True when text clearly mentions one of the target engineering roles."""
    return bool(text and TARGET_POST_ROLE_RE.search(text))


def extract_recruitment_emails(text: str, links: list[str] | None = None) -> list[str]:
    """Extract real recruitment email addresses from post text and mailto links."""
    candidates: list[str] = []
    for href in links or []:
        clean_href = (href or "").strip()
        if clean_href.lower().startswith("mailto:"):
            addr = unquote(clean_href[7:].split("?", 1)[0]).strip()
            if addr:
                candidates.append(addr)
    for m in _EMAIL_RE.finditer(text or ""):
        candidates.append(m.group(0).strip())

    seen: set[str] = set()
    out: list[str] = []
    for raw in candidates:
        addr = raw.strip(".,;:)>]\"'").lower()
        if "@" not in addr:
            continue
        local, domain = addr.split("@", 1)
        if any(domain.endswith(ext) or addr.endswith(ext) for ext in _ASSET_SUFFIXES):
            continue
        if local in _PLACEHOLDER_LOCAL_PARTS or domain in _PLACEHOLDER_DOMAINS:
            continue
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def extract_email_subject_hint(text: str) -> str:
    """Extract an explicitly requested email subject line from a hiring post, if present."""
    if not text:
        return ""
    m = _SUBJECT_HINT_RE.search(text)
    if not m:
        return ""
    return m.group(1).strip(" .:-–\"'“”")


def is_hiring_post(text: str) -> bool:
    """Classify whether a LinkedIn post is a real vacancy in one of the target roles."""
    clean = (text or "").strip()
    if len(clean) < 40:
        return False
    if _JOB_SEEKER_RE.search(clean):
        return False
    if not is_target_role_post(clean):
        return False
    if _HIRING_SIGNAL_RE.search(clean):
        return True
    emails = extract_recruitment_emails(clean)
    return bool(emails and _HIRING_EMAIL_CONTEXT_RE.search(clean))


def infer_post_role_title(text: str) -> str:
    """Extract a concise role title from a hiring post or synthesize one from matched keywords."""
    lines = [ln.strip(" •-*🔹🚀✅🔥📢💻\t") for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if 4 <= len(ln) <= 110]
    for ln in lines[:12]:
        low = ln.lower()
        if any(skip in low for skip in ("publicação no feed", "feed post", "seguir", "follow", "hiring!", "estamos contratando")):
            if ":" not in ln and "-" not in ln:
                continue
        if any(noise in low for noise in ("required.", "background required", "comenta ", "me chama", "enviar currículo", "envie seu")):
            continue
        if TARGET_POST_ROLE_RE.search(ln) and not ln.lower().startswith(("http", "#", "requisitos", "responsabilidades", "benefícios")):
            cleaned = re.sub(
                r"^(?:vaga(?:\s+aberta)?\s*[:\-–]\s*|oportunidade\s*[:\-–]\s*|we(?:'re|\s+are)\s+hiring\s+(?:an?|for)\s+|estamos\s+contratando\s+)",
                "",
                ln,
                flags=re.I,
            ).strip()
            cleaned = re.split(r"\s+(?:to\s+work\s+on|para\s+atuar)\b", cleaned, maxsplit=1, flags=re.I)[0].strip()
            if 4 <= len(cleaned) <= 90:
                return cleaned

    low = (text or "").lower()
    if re.search(r"forward(?:ed)?\s+deploy|\bfde\b", low):
        return "Forward Deployed Engineer (Post LinkedIn)"
    if re.search(r"dev\s+ia|ai\s+engineer|engenheir[oa]\s+(?:de\s+)?ia|llm|genai", low):
        return "AI Engineer / Dev IA (Post LinkedIn)"
    if re.search(r"full[- ]?stack", low):
        return "Desenvolvedor(a) Fullstack (Post LinkedIn)"
    if re.search(r"front[- ]?end|react|next\.?js", low):
        return "Desenvolvedor(a) Front-end (Post LinkedIn)"
    return "Software Engineer (Post LinkedIn)"


def _unwrap_linkedin_href(href: str) -> str:
    clean = (href or "").strip()
    if "/safety/go/" in clean:
        qs = parse_qs(urlparse(clean).query)
        if qs.get("url"):
            return unquote(qs["url"][0]).strip()
    return clean


def _is_external_job_url(url: str) -> bool:
    clean = (url or "").strip()
    if not clean.startswith(("http://", "https://")):
        return False
    host = (urlparse(clean).netloc or "").lower()
    if "linkedin.com" in host:
        return "/jobs/view/" in clean
    return True


def _pick_best_external_url(urls: list[str]) -> str:
    candidates = [u for u in urls if _is_external_job_url(u)]
    if not candidates:
        return ""
    for u in candidates:
        plat = detect_platform(u)
        if plat not in ("generic", "linkedin"):
            return u
    return candidates[0]


def _extract_company_from_post(text: str, author: str, links: list[str]) -> str:
    m = re.search(r"(?:empresa|company)\s*[:\-–]\s*([^\n|]{2,60})", text or "", re.IGNORECASE)
    if m:
        return m.group(1).strip()
    if author.strip():
        return author.strip()
    for ln in links:
        if "linkedin.com/company/" in ln.lower():
            slug = urlparse(ln).path.strip("/").split("/")[-1]
            if slug:
                return slug.replace("-", " ").title()
    return ""


class LinkedInPostsScraper(JobSource):
    """Scrapes recent LinkedIn feed posts advertising engineering vacancies."""

    name = "linkedin_posts"

    def __init__(self, browser, queries: list[str] | None = None, *, scroll_rounds: int = 1):
        self._browser = browser
        configured = queries if queries is not None else env_list("LINKEDIN_POST_QUERIES")
        self._queries = [q.strip() for q in configured if q.strip()] or list(DEFAULT_POST_QUERIES)
        self._scroll_rounds = max(0, scroll_rounds)
        self.session_lost = False

    def scrape(self) -> list[dict]:
        if self._browser is None:
            return []
        jobs: list[dict] = []
        seen: set[str] = set()
        for query in self._queries:
            url = self._search_url(query)
            html = self._page_html(url)
            if not html:
                if self.session_lost:
                    break
                continue
            for job in self._parse_posts(html, search_url=url):
                if job["id"] not in seen:
                    seen.add(job["id"])
                    jobs.append(job)
        return jobs

    @staticmethod
    def _search_url(query: str) -> str:
        return (
            "https://www.linkedin.com/search/results/content/"
            f"?keywords={quote_plus(query)}"
            '&sortBy=%22date_posted%22&datePosted=%22past-week%22&origin=FACETED_SEARCH'
        )

    def _page_html(self, url: str) -> str | None:
        from bot.scrapers.browser import goto, polite_sleep

        try:
            with goto(self._browser, url, timeout_ms=30000, block_heavy_assets=True) as page:
                if page is None:
                    return None
                if any(tok in page.url for tok in ("/authwall", "/login", "/checkpoint")):
                    self.session_lost = True
                    logger.warning("LinkedIn session lost while scanning feed posts")
                    return None
                try:
                    page.wait_for_selector(
                        '[componentkey^="expanded"][componentkey$="FeedType_FLAGSHIP_SEARCH"], '
                        '[data-testid="expandable-text-box"], .feed-shared-update-v2',
                        timeout=10000,
                    )
                except PWTimeout:
                    return page.content()
                for _ in range(self._scroll_rounds):
                    try:
                        page.mouse.wheel(0, 2000)
                        page.wait_for_timeout(1200)
                    except Exception:  # noqa: BLE001
                        break
                html = page.content()
                polite_sleep(1.5, 2.5)
                return html
        except PWTimeout:
            logger.warning("Timed out loading LinkedIn content search for %s", url)
            return None

    def _parse_posts(self, html: str, search_url: str = "") -> list[dict]:
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select('[componentkey^="expanded"][componentkey$="FeedType_FLAGSHIP_SEARCH"]')
        if not cards:
            cards = soup.select('.feed-shared-update-v2, [data-urn^="urn:li:activity:"]')

        out: list[dict] = []
        seen_ids: set[str] = set()
        for card in cards:
            text_box = card.select_one('[data-testid="expandable-text-box"]') or card.select_one(
                ".feed-shared-update-v2__description"
            )
            post_text = text_box.get_text("\n", strip=True) if text_box else card.get_text("\n", strip=True)
            post_text = re.sub(r"(?:\s*[…\.]*\s*(?:mais|more)\s*)+$", "", post_text, flags=re.I).strip()
            if not is_hiring_post(post_text):
                continue

            raw_hrefs = [a.get("href", "") for a in card.select("a[href]") if a.get("href")]
            all_links = [_unwrap_linkedin_href(h) for h in raw_hrefs]
            all_links.extend(u.rstrip(".,);") for u in _URL_RE.findall(post_text))

            emails = extract_recruitment_emails(post_text, all_links)
            subject_hint = extract_email_subject_hint(post_text)
            best_ext_url = _pick_best_external_url(all_links)

            author_name = ""
            author_profile_url = ""
            permalink = ""
            for a in card.select("a[href]"):
                href = _unwrap_linkedin_href(a.get("href", ""))
                low_href = href.lower()
                if ("/feed/update/" in low_href or "/posts/" in low_href) and not permalink:
                    permalink = href.split("?")[0]
                if ("linkedin.com/in/" in low_href or "linkedin.com/company/" in low_href) and not author_profile_url:
                    author_profile_url = href.split("?")[0]
                    txt = a.get_text(" ", strip=True)
                    if txt and len(txt) <= 80:
                        author_name = txt.split("•")[0].strip()

            if not author_name:
                header_lines = [
                    ln.strip()
                    for ln in card.get_text("\n", strip=True).splitlines()[:8]
                    if ln.strip() and ln.strip().lower() not in ("publicação no feed", "feed post", "seguir", "follow")
                ]
                if header_lines:
                    author_name = header_lines[0][:60]

            ck = (card.get("componentkey") or card.get("data-urn") or "").strip()
            if ck.startswith("expanded") and ck.endswith("FeedType_FLAGSHIP_SEARCH"):
                post_key = ck[len("expanded") : -len("FeedType_FLAGSHIP_SEARCH")]
            elif ck:
                post_key = re.sub(r"[^A-Za-z0-9_-]+", "", ck)
            else:
                post_key = hashlib.sha1(post_text.encode("utf-8")).hexdigest()[:24]
            job_id = f"lip:{post_key[:28]}"
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            if emails:
                subject_qs = f"?subject={quote_plus(subject_hint)}" if subject_hint else ""
                apply_url = f"mailto:{emails[0]}{subject_qs}"
            elif best_ext_url:
                apply_url = best_ext_url
            else:
                apply_url = ""

            if permalink:
                view_url = permalink
            elif author_profile_url and ("linkedin.com/in/" in author_profile_url or "linkedin.com/company/" in author_profile_url):
                view_url = f"{author_profile_url.rstrip('/')}/recent-activity/all/"
            else:
                view_url = best_ext_url or search_url

            title = infer_post_role_title(post_text)
            company = _extract_company_from_post(post_text, author_name, all_links)
            is_remote = bool(_REMOTE_RE.search(post_text))
            location = "Remoto" if is_remote else "Brasil"

            out.append({
                "id": job_id,
                "source": self.name,
                "url": view_url,
                "apply_url": apply_url,
                "title": title,
                "company": company,
                "location": location,
                "modality": "Remote" if is_remote else "",
                "seniority": "",
                "remote": is_remote,
                "salary": "",
                "posted_at": "",
                "body": post_text[:8000],
            })
        return out
