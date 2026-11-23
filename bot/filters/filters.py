"""Cheap rule-based pre-filters, deduplication hashing, and job language detection."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from bot.config import env_list

logger = logging.getLogger("jobbot.filters")

_TITLE_NOISE = re.compile(r"[^a-z0-9]+")

_PT_MARKERS = re.compile(
    r"\b(vaga|empresa|requisitos|benefícios|desejável|obrigatório|experiência|"
    r"desenvolvedor|desenvolvimento|conhecimento|contratação|remoto|híbrido|"
    r"presencial|salário|responsabilidades|atuar|equipe|tecnologias|pleno|sênior)\b",
    re.IGNORECASE,
)
_EN_MARKERS = re.compile(
    r"\b(requirements|responsibilities|qualifications|benefits|experience|"
    r"software engineer|developer|preferred|required|salary|remote|hybrid|"
    r"full-time|apply|team|we are looking|about the role|must have|nice to have)\b",
    re.IGNORECASE,
)

# Phrases that disqualify Brazil/LATAM remote applicants on international boards
INTERNATIONAL_BLOCKED_PHRASES: tuple[str, ...] = (
    "us only",
    "u.s. only",
    "usa only",
    "united states only",
    "us-only",
    "u.s.-only",
    "us citizen only",
    "us citizens only",
    "u.s. citizen only",
    "u.s. citizens only",
    "united states citizens only",
    "must be authorized to work in the us",
    "must be authorized to work in the u.s",
    "must be authorized to work in the united states",
    "authorized to work in the us without",
    "authorized to work in the united states",
    "no visa sponsorship",
    "without visa sponsorship",
    "not able to sponsor visas",
    "unable to sponsor",
    "w2 only",
    "w-2 only",
    "must reside in the us",
    "must reside in the u.s",
    "must reside in the united states",
    "must be located in the us",
    "must be located in the united states",
    "us/canada only",
    "us & canada only",
    "us and canada only",
    "canada only",
    "uk only",
    "eu only",
    "europe only",
    "emea only",
    "apac only",
    "australia only",
)

# Positive signals for international remote roles open to Brazil / LATAM contractors
INTERNATIONAL_POSITIVE_TERMS: tuple[str, ...] = (
    "latam",
    "latin america",
    "worldwide",
    "anywhere",
    "contractor",
    "b2b",
    "americas",
    "south america",
    "brazil",
    "brasil",
    "global",
)

_POSITIVE_LOCATION_RE = re.compile(
    r"\b(latam|latin america|worldwide|anywhere|global|americas|south america|brazil|brasil)\b",
    re.IGNORECASE,
)

_FOREIGN_ONLY_LOCATION_RE = re.compile(
    r"\b(usa|u\.s\.a\.?|united states|us|canada|europe|eu|emea|uk|united kingdom|"
    r"great britain|england|germany|france|spain|italy|netherlands|poland|portugal|"
    r"ireland|sweden|switzerland|austria|belgium|denmark|norway|finland|czech|"
    r"australia|new zealand|apac|asia|india|singapore|japan|israel|africa)\b",
    re.IGNORECASE,
)


def international_fit_score(job: dict) -> int:
    """Count positive international/LATAM/Worldwide signals to prioritize open remote roles."""
    haystack = " ".join(
        str(job.get(k) or "") for k in ("title", "location", "body")
    ).lower()
    if not haystack.strip():
        return 0
    return sum(1 for term in INTERNATIONAL_POSITIVE_TERMS if term in haystack)


def _is_foreign_restricted_location(location: str) -> str | None:
    """Return a block reason if location explicitly restricts to non-LATAM/non-Brazil regions."""
    loc = (location or "").strip()
    if not loc:
        return None
    if _POSITIVE_LOCATION_RE.search(loc):
        return None
    low = loc.lower()
    for phrase in INTERNATIONAL_BLOCKED_PHRASES:
        if phrase in low:
            return f"restricted location: {phrase}"
    # If location lists specific foreign countries/regions without any LATAM/Worldwide/Brazil mention
    if _FOREIGN_ONLY_LOCATION_RE.search(loc):
        # Ignore generic Brazilian city/state formats or plain "Remote"
        if re.search(r"\b(remoto|sp|rj|mg|pe|pr|rs|sc|ba|ce|df|go|recife|s[aã]o paulo|rio de janeiro|curitiba|belo horizonte|florian[oó]polis|porto alegre)\b", low):
            return None
        return f"restricted location: {loc[:60]}"
    return None


def content_hash(title: str, company: str) -> str:
    t = _TITLE_NOISE.sub("", (title or "").lower())
    c = _TITLE_NOISE.sub("", (company or "").lower())
    return hashlib.sha1(f"{t}|{c}".encode()).hexdigest()


def is_too_old(posted_at: str | int | float | None, max_age_days: int) -> bool:
    if posted_at is None or posted_at == "" or isinstance(posted_at, bool):
        return False
    try:
        dt: datetime
        if isinstance(posted_at, (int, float)):
            ts = float(posted_at)
            if ts > 1e12:
                ts /= 1000.0
            if ts <= 0:
                return False
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif isinstance(posted_at, str):
            raw = posted_at.strip()
            if not raw:
                return False
            if re.fullmatch(r"\d+(?:\.\d+)?", raw):
                ts = float(raw)
                if ts > 1e12:
                    ts /= 1000.0
                if ts <= 0:
                    return False
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            return False

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt > timedelta(days=max_age_days)
    except (ValueError, TypeError, OSError, OverflowError):
        return False


def detect_language(title: str = "", body: str = "", default: str = "pt") -> str:
    """Heuristically detect whether a job posting is in Portuguese ('pt') or English ('en')."""
    text = f"{title or ''}\n{(body or '')[:4000]}"
    pt_score = len(_PT_MARKERS.findall(text))
    en_score = len(_EN_MARKERS.findall(text))
    if pt_score == 0 and en_score == 0:
        return (default or "pt").lower()[:2]
    if en_score > pt_score * 1.2:
        return "en"
    if pt_score >= en_score:
        return "pt"
    return (default or "pt").lower()[:2]


def passes_pre_filters(job: dict, blocked_title_keywords: list[str] | None = None) -> tuple[bool, str]:
    """Cheap rule-based filters. Returns (ok, reason)."""
    blocked_title_keywords = (
        blocked_title_keywords if blocked_title_keywords is not None else env_list("BLOCKED_TITLE_KEYWORDS")
    )
    title_l = (job.get("title") or "").lower()
    company_l = (job.get("company") or "").lower()

    for kw in env_list("BLOCKED_COMPANIES"):
        if kw.lower() in company_l:
            return False, f"blocked company: {kw}"

    for kw in blocked_title_keywords:
        if kw.lower() in title_l:
            return False, f"blocked keyword: {kw}"

    blocked = ("estágio", "estagiário", "intern", "trainee", "junior de tudo")
    for kw in blocked:
        if kw in title_l:
            return False, f"blocked level: {kw}"

    loc_reason = _is_foreign_restricted_location(job.get("location") or "")
    if loc_reason:
        return False, loc_reason

    combined = f"{title_l}\n{(job.get('location') or '').lower()}\n{(job.get('body') or '').lower()}"
    for phrase in INTERNATIONAL_BLOCKED_PHRASES:
        if phrase in combined:
            return False, f"international restriction: {phrase}"

    return True, ""
