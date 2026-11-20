"""Job compatibility analysis using Gemini 4 Neon (Medium) via agy CLI."""

from __future__ import annotations

import json
import logging
from typing import Any

from bot.filters.filters import detect_language
from bot.gemini.client import agy_ask, agy_available, extract_json_object

logger = logging.getLogger("jobbot.gemini.analysis")

ANALYSIS_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "match": {"type": "integer", "minimum": 0, "maximum": 100},
        "recommendation": {"type": "string", "enum": ["apply", "maybe", "skip"]},
        "language": {"type": "string", "enum": ["pt", "en", "es", "other"]},
        "seniority": {"type": "string"},
        "has_eliminatory_blocker": {"type": "boolean"},
        "eliminatory_blockers": {"type": "array", "items": {"type": "string"}},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "missing_required_skills": {"type": "array", "items": {"type": "string"}},
        "missing_skills": {"type": "array", "items": {"type": "string"}},
        "experience_fit": {"type": "string"},
        "location_fit": {"type": "string"},
        "salary_fit": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "resume_key": {"type": "string"},
        "resume_changes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "match",
        "recommendation",
        "language",
        "has_eliminatory_blocker",
        "eliminatory_blockers",
        "matched_skills",
        "missing_required_skills",
        "missing_skills",
        "reasons",
        "resume_key",
        "resume_changes",
    ],
}

# Backwards-compatible alias
JSON_SCHEMA = json.dumps(ANALYSIS_JSON_SCHEMA)


def should_apply(analysis: dict, threshold: int = 75) -> tuple[bool, str]:
    """Decide whether a job passes compatibility and eliminatory requirements."""
    if not analysis:
        return False, "análise ausente"
    blockers = [b for b in (analysis.get("eliminatory_blockers") or []) if str(b).strip()]
    if analysis.get("has_eliminatory_blocker") or blockers:
        summary = "; ".join(blockers[:3]) if blockers else "requisito eliminatório não atendido"
        return False, f"requisito eliminatório: {summary}"
    score = int(analysis.get("match") or 0)
    if score < threshold:
        return False, f"compatibilidade {score}% abaixo do mínimo ({threshold}%)"
    rec = analysis.get("recommendation")
    if rec == "skip":
        return False, "recomendação skip pela análise de compatibilidade"
    return True, ""


class JobAnalyzer:
    """Analyzes job fit via the agy CLI (Gemini 4 Neon, Medium effort by default)."""

    def __init__(self, resume_library, candidate_profile=None, default_language: str = "pt"):
        self._resumes = resume_library
        self._profile = candidate_profile
        self._default_language = (default_language or "pt").lower()[:2]
        self._ready = agy_available()
        if not self._ready:
            logger.warning("agy not available — analysis disabled, jobs stay 'discovered'")

    @property
    def ready(self) -> bool:
        return self._ready

    def analyze(self, job: dict) -> dict | None:
        if not self._ready:
            return None
        from bot.gemini.prompts import ANALYSIS_SYSTEM, ANALYSIS_USER

        prompt = f"{ANALYSIS_SYSTEM}\n\n{ANALYSIS_USER.format(**self._prompt_kwargs(job))}"
        try:
            raw = agy_ask(prompt, json_schema=ANALYSIS_JSON_SCHEMA)
        except Exception as exc:  # noqa: BLE001
            logger.error("agy analysis failed job=%s err=%s", job.get("id"), exc)
            return None

        data = self._parse(raw, job)
        if data is None:
            return None
        data["resume_key"] = self._validate_resume_key(data.get("resume_key"), data.get("language", "pt"))
        return data

    def _prompt_kwargs(self, job: dict) -> dict[str, Any]:
        profile_summary = ""
        if self._profile is not None:
            profile_summary = self._profile.as_verified_facts_text()[:3500]
        return {
            "profile_summary": profile_summary or "(perfil consolidado não gerado; usar os currículos abaixo)",
            "resumes": self._resumes.as_prompt_block(),
            "resume_count": self._resumes.count,
            "source": job.get("source", ""),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "modality": job.get("modality") or ("Remoto" if job.get("remote") else ""),
            "seniority": job.get("seniority", ""),
            "salary": job.get("salary", "") or "Não informado",
            "url": job.get("url", ""),
            "body": (job.get("body") or "")[:7500],
        }

    def _parse(self, raw: str, job: dict | None = None) -> dict | None:
        data = extract_json_object(raw)
        if data is None:
            logger.warning("agy returned invalid/non-JSON analysis response")
            return None

        score = data.get("match")
        if isinstance(score, float):
            score = round(score)
        if not isinstance(score, int) or not 0 <= score <= 100:
            return None
        data["match"] = score

        blockers = [str(x).strip()[:200] for x in (data.get("eliminatory_blockers") or []) if str(x).strip()][:6]
        has_blocker = bool(data.get("has_eliminatory_blocker")) or bool(blockers)
        data["eliminatory_blockers"] = blockers
        data["has_eliminatory_blocker"] = has_blocker

        rec = data.get("recommendation")
        if has_blocker:
            rec = "skip"
        elif rec not in ("apply", "maybe", "skip"):
            rec = "apply" if score >= 75 else "maybe" if score >= 60 else "skip"
        data["recommendation"] = rec

        lang = str(data.get("language") or "").lower().strip()
        if lang not in ("pt", "en"):
            lang = detect_language(
                (job or {}).get("title", ""),
                (job or {}).get("body", ""),
                default=self._default_language,
            )
        data["language"] = lang

        for str_field in ("seniority", "experience_fit", "location_fit", "salary_fit"):
            data[str_field] = str(data.get(str_field) or "").strip()[:200]

        for list_field in (
            "matched_skills",
            "missing_required_skills",
            "missing_skills",
            "reasons",
            "resume_changes",
        ):
            data[list_field] = [
                str(x).strip()[:180] for x in (data.get(list_field) or []) if str(x).strip()
            ][:10]

        # Merge missing_required_skills into missing_skills if missing_skills is empty
        if data["missing_required_skills"] and not data["missing_skills"]:
            data["missing_skills"] = list(data["missing_required_skills"])

        allowed = {
            "match",
            "recommendation",
            "language",
            "seniority",
            "has_eliminatory_blocker",
            "eliminatory_blockers",
            "matched_skills",
            "missing_required_skills",
            "missing_skills",
            "experience_fit",
            "location_fit",
            "salary_fit",
            "reasons",
            "resume_key",
            "resume_changes",
        }
        return {k: v for k, v in data.items() if k in allowed}

    def _validate_resume_key(self, key: str | None, language: str = "pt") -> str | None:
        valid = self._resumes.keys()
        if not valid:
            return None
        chosen = None
        if key in valid:
            chosen = key
        else:
            lowered = (key or "").lower()
            for v in valid:
                if lowered and (lowered in v.lower() or v.lower() in lowered):
                    chosen = v
                    break
        if chosen is None:
            logger.warning("AI returned unknown resume_key %r, using language-matched fallback", key)
        return self._resumes.best_key_for_language(chosen, language)
