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
    ("inhire", ("inhire.app",)),
    ("recruitee", ("recruitee.com",)),
    ("workable", ("apply.workable.com", "workable.com")),
    ("linkedin", ("linkedin.com",)),
    ("programathor", ("programathor.com.br",)),
    ("geekhunter", ("geekhunter.com", "geekhunter.com.br")),
    ("nerdin", ("nerdin.com.br",)),
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
    for platform, suffixes in PLATFORM_HOST_RULES:
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return platform
    if source in ("linkedin", "programathor", "geekhunter", "nerdin") and not host:
        return source
    return "generic"


def resolve_apply_url(job: dict, browser_context=None, timeout_ms: int = 25000) -> tuple[str, str]:
    """Resolve the direct application URL and detected platform for a job."""
    url = (job.get("apply_url") or job.get("url") or "").strip()
    source = (job.get("source") or "").strip().lower()
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
            count = min(anchors.count(), 40)
            for i in range(count):
                el = anchors.nth(i)
                try:
                    text = (el.inner_text(timeout=500) or "").strip()
                    href = (el.get_attribute("href", timeout=500) or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                    continue
                if _APPLY_LINK_TEXT.search(text):
                    candidate = urljoin(final_url, href)
                    cand_platform = detect_platform(candidate, source)
                    return candidate, cand_platform
    except Exception as exc:  # noqa: BLE001
        logger.debug("resolve_apply_url fallback for %s: %s", url, exc)

    return url, platform
