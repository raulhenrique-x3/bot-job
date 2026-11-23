"""Recruitment platform detection and application URL resolution."""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

logger = logging.getLogger("jobbot.appliers.detector")

PLATFORM_HOST_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("greenhouse", ("greenhouse.io",)),
    ("lever", ("lever.co",)),
    ("ashby", ("ashbyhq.com",)),
    ("gupy", ("gupy.io",)),
    ("workday", ("myworkdayjobs.com", "myworkdaysite.com")),
    ("smartrecruiters", ("smartrecruiters.com",)),
    ("bamboohr", ("bamboohr.com",)),
    ("solides", ("solides.jobs", "vagas.solides.com.br")),
    ("inhire", ("inhire.app", "carreira.inhire.com.br", "inhire.com.br")),
    ("recruitee", ("recruitee.com",)),
    ("workable", ("apply.workable.com", "workable.com")),
    ("linkedin", ("linkedin.com",)),
    ("programathor", ("programathor.com.br",)),
    ("geekhunter", ("geekhunter.com", "geekhunter.com.br")),
    ("nerdin", ("nerdin.com.br",)),
    ("wellfound", ("wellfound.com", "angel.co")),
    ("avanade", ("avanade.com",)),
)

_APPLY_LINK_TEXT = re.compile(
    r"candidatar|apply|inscrever|quero me candidatar|enviar curr[ií]culo|apply now|easy apply",
    re.IGNORECASE,
)


def detect_platform(url: str, source: str = "") -> str:
    """Identify the recruitment platform hosting the application form."""
    clean = (url or "").strip()
    if clean.lower().startswith("mailto:"):
        return "email"
    parsed = urlparse(clean)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if (host == "myworkdayjobs.com" or host.endswith(".myworkdayjobs.com")) and "/avanadecareers" in path:
        return "avanade"
    for platform, suffixes in PLATFORM_HOST_RULES:
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return platform
    if source in ("linkedin", "programathor", "geekhunter", "nerdin", "wellfound", "avanade") and not host:
        return source
    return "generic"


def resolve_apply_url(job: dict, browser_context=None, timeout_ms: int = 25000) -> tuple[str, str]:
    """Resolve the direct application URL and detected platform for a job."""
    source = (job.get("source") or "").strip().lower()
    raw_apply = (job.get("apply_url") or "").strip()
    if source == "linkedin_posts" and (
        not raw_apply
        or "/recent-activity/" in raw_apply.lower()
        or "/search/results/" in raw_apply.lower()
    ):
        return "", "linkedin_post"

    url = (job.get("apply_url") or job.get("url") or "").strip()
    if not url:
        return "", "generic"

    platform = detect_platform(url, source)
    # Direct ATS and native portal URLs need no extra aggregator indirection
    if platform not in ("querovagas", "devagas", "generic"):
        return url, platform

    # If the scraper already gave a direct external ATS URL, keep it
    if browser_context is None or platform == "email":
        return url, platform

    # Best-effort discovery of an outbound apply link on aggregator pages
    from bot.scrapers.browser import goto

    try:
        with goto(browser_context, url, timeout_ms=timeout_ms) as page:
            if page is None:
                return url, platform
            final_url = page.url or url
            final_platform = detect_platform(final_url, source)
            if final_platform != "generic":
                return final_url, final_platform
            anchors = page.locator("a[href]")
            count = min(anchors.count(), 80)
            direct_ats_candidate: tuple[str, str] | None = None
            apply_link_candidate: tuple[str, str] | None = None
            for i in range(count):
                el = anchors.nth(i)
                try:
                    href = (el.get_attribute("href", timeout=500) or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                    continue
                candidate = urljoin(final_url, href)
                cand_platform = detect_platform(candidate, source)
                if cand_platform in ("greenhouse", "lever", "ashby", "inhire", "gupy", "workable", "smartrecruiters", "bamboohr"):
                    direct_ats_candidate = (candidate, cand_platform)
                    break
                if apply_link_candidate is None:
                    try:
                        text = (el.inner_text(timeout=500) or "").strip()
                    except Exception:  # noqa: BLE001
                        text = ""
                    cand_host = (urlparse(candidate).netloc or "").lower()
                    is_lnkd_outbound = (
                        "lnkd.in" in (urlparse(final_url).netloc or "").lower()
                        and candidate.startswith(("http://", "https://"))
                        and "linkedin.com" not in cand_host
                        and "lnkd.in" not in cand_host
                    )
                    if _APPLY_LINK_TEXT.search(text) or is_lnkd_outbound:
                        apply_link_candidate = (candidate, cand_platform)

            if direct_ats_candidate is not None:
                return direct_ats_candidate

            if apply_link_candidate is not None:
                cand_url, cand_platform = apply_link_candidate
                if cand_platform != "generic":
                    return cand_url, cand_platform
                # Follow one redirect hop for aggregator outbound links (e.g. /go/, /apply)
                if cand_url.split("#", 1)[0].rstrip("/") != final_url.split("#", 1)[0].rstrip("/"):
                    try:
                        page.goto(cand_url, wait_until="domcontentloaded", timeout=min(timeout_ms, 15000))
                        hop_url = page.url or cand_url
                        return hop_url, detect_platform(hop_url, source)
                    except Exception:  # noqa: BLE001
                        return cand_url, cand_platform
    except Exception as exc:  # noqa: BLE001
        logger.debug("resolve_apply_url fallback for %s: %s", url, exc)

    return url, platform
