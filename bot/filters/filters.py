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


def content_hash(title: str, company: str) -> str:
    t = _TITLE_NOISE.sub("", (title or "").lower())
    c = _TITLE_NOISE.sub("", (company or "").lower())
    return hashlib.sha1(f"{t}|{c}".encode()).hexdigest()


def is_too_old(posted_at: str | None, max_age_days: int) -> bool:
    if not posted_at:
        return False
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt > timedelta(days=max_age_days)
    except ValueError:
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

    return True, ""
