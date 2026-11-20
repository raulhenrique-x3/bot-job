"""Tailored ATS resume generator grounded strictly in verified candidate resumes."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from bot.documents.pdf_renderer import render_resume_pdf, validate_pdf
from bot.documents.storage import ApplicationStorage
from bot.gemini.client import agy_ask, extract_json_object
from bot.gemini.prompts import TAILORED_RESUME_SYSTEM, TAILORED_RESUME_USER
from bot.profile import CandidateProfile, ResumeLibrary

logger = logging.getLogger("jobbot.documents.resume")

_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

RESUME_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "full_name": {"type": "string"},
        "headline": {"type": "string"},
        "contact_line": {"type": "string"},
        "summary": {"type": "string"},
        "experiences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "location": {"type": "string"},
                    "period": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["company", "role", "period", "bullets"],
            },
        },
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["category", "items"],
            },
        },
        "projects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "description"],
            },
        },
        "education": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "degree": {"type": "string"},
                    "institution": {"type": "string"},
                    "period": {"type": "string"},
                },
                "required": ["degree", "institution"],
            },
        },
        "certifications": {"type": "array", "items": {"type": "string"}},
        "languages": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["full_name", "headline", "contact_line", "summary", "experiences", "skills", "education"],
}

_SECTION_HEADINGS = {
    "pt": {
        "summary": "RESUMO PROFISSIONAL",
        "experience": "EXPERIÊNCIA PROFISSIONAL",
        "skills": "COMPETÊNCIAS TÉCNICAS",
        "projects": "PROJETOS RELEVANTES",
        "education": "FORMAÇÃO ACADÊMICA",
        "certifications": "CERTIFICAÇÕES",
        "languages": "IDIOMAS",
    },
    "en": {
        "summary": "PROFESSIONAL SUMMARY",
        "experience": "PROFESSIONAL EXPERIENCE",
        "skills": "TECHNICAL SKILLS",
        "projects": "SELECTED PROJECTS",
        "education": "EDUCATION",
        "certifications": "CERTIFICATIONS",
        "languages": "LANGUAGES",
    },
}


@dataclass
class GeneratedResume:
    job_id: str
    base_resume_key: str
    language: str
    md_path: Path
    pdf_path: Path
    markdown: str
    data: dict
    warnings: list[str]


def build_contact_line(profile: CandidateProfile) -> str:
    parts = [
        p
        for p in (
            profile.location,
            profile.phone,
            profile.email,
            profile.linkedin_url.replace("https://", "").replace("http://", "") if profile.linkedin_url else "",
            profile.github_url.replace("https://", "").replace("http://", "") if profile.github_url else "",
        )
        if p
    ]
    return " | ".join(parts)


def resume_dict_to_markdown(data: dict, language: str = "pt") -> str:
    """Serialize structured ATS resume into clean, single-column Markdown (no tables/icons)."""
    lang = "en" if (language or "pt").lower().startswith("en") else "pt"
    h = _SECTION_HEADINGS[lang]
    lines: list[str] = []

    if data.get("full_name"):
        lines.append(f"# {data['full_name'].strip()}")
    if data.get("headline"):
        lines.append(f"**{data['headline'].strip()}**")
    if data.get("contact_line"):
        lines.append(data["contact_line"].strip())
    lines.append("")

    if data.get("summary"):
        lines.append(f"## {h['summary']}")
        lines.append(data["summary"].strip())
        lines.append("")

    if data.get("experiences"):
        lines.append(f"## {h['experience']}")
        for exp in data["experiences"]:
            role = (exp.get("role") or "").strip()
            company = (exp.get("company") or "").strip()
            location = (exp.get("location") or "").strip()
            period = (exp.get("period") or "").strip()
            lines.append(f"### {role} | {company}".strip(" |"))
            meta = " | ".join(p for p in (period, location) if p)
            if meta:
                lines.append(f"*{meta}*")
            for b in exp.get("bullets") or []:
                cleaned = str(b).strip().lstrip("-•●* ")
                if cleaned:
                    lines.append(f"- {cleaned}")
            lines.append("")

    if data.get("skills"):
        lines.append(f"## {h['skills']}")
        for group in data["skills"]:
            if isinstance(group, dict):
                cat = (group.get("category") or "").strip()
                items = [str(i).strip() for i in (group.get("items") or []) if str(i).strip()]
                if items:
                    lines.append(f"- **{cat}:** {', '.join(items)}" if cat else f"- {', '.join(items)}")
            elif str(group).strip():
                lines.append(f"- {str(group).strip()}")
        lines.append("")

    if data.get("projects"):
        lines.append(f"## {h['projects']}")
        for proj in data["projects"]:
            if isinstance(proj, dict):
                name = (proj.get("name") or "").strip()
                desc = (proj.get("description") or "").strip()
                if name:
                    lines.append(f"- **{name}:** {desc}" if desc else f"- **{name}**")
        lines.append("")

    if data.get("education"):
        lines.append(f"## {h['education']}")
        for edu in data["education"]:
            if isinstance(edu, dict):
                line = " | ".join(
                    p for p in ((edu.get("degree") or "").strip(), (edu.get("institution") or "").strip(), (edu.get("period") or "").strip()) if p
                )
                if line:
                    lines.append(f"- {line}")
        lines.append("")

    if data.get("certifications"):
        lines.append(f"## {h['certifications']}")
        for cert in data["certifications"]:
            if str(cert).strip():
                lines.append(f"- {str(cert).strip()}")
        lines.append("")

    if data.get("languages"):
        lines.append(f"## {h['languages']}")
        langs = [str(x).strip() for x in data["languages"] if str(x).strip()]
        if langs:
            lines.append(", ".join(langs))
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def validate_tailored_resume(
    data: dict,
    *,
    verified_years: set[str],
    expected_name: str = "",
) -> tuple[bool, list[str]]:
    """Verify structural integrity and guard against hallucinated dates or empty sections."""
    warnings: list[str] = []
    if not data.get("summary") or not data.get("experiences"):
        return False, ["Currículo gerado sem resumo ou experiências."]

    if expected_name and expected_name.lower() not in (data.get("full_name") or "").lower():
        warnings.append("Nome do candidato ajustado para o nome verificado no perfil.")
        data["full_name"] = expected_name

    # Verify that any 4-digit years in experiences/education appear in source resumes
    if verified_years:
        generated_years: set[str] = set()
        for exp in data.get("experiences") or []:
            generated_years.update(_YEAR_RE.findall(str(exp.get("period") or "")))
        for edu in data.get("education") or []:
            if isinstance(edu, dict):
                generated_years.update(_YEAR_RE.findall(str(edu.get("period") or "")))
        unverified = sorted(generated_years - verified_years)
        if unverified:
            return False, [f"Anos não comprovados nos currículos originais: {', '.join(unverified)}"]

    return True, warnings


class TailoredResumeGenerator:
    """Generates a tailored ATS resume (.md + .pdf) per job application."""

    def __init__(
        self,
        resume_library: ResumeLibrary,
        profile: CandidateProfile,
        storage: ApplicationStorage,
        default_language: str = "pt",
    ):
        self._resumes = resume_library
        self._profile = profile
        self._storage = storage
        self._default_language = (default_language or "pt").lower()[:2]

    def generate(self, job: dict, analysis: dict) -> GeneratedResume:
        job_id = job["id"]
        target_lang = (analysis.get("language") or self._default_language or "pt").lower()[:2]
        if target_lang not in ("pt", "en"):
            target_lang = self._default_language if self._default_language in ("pt", "en") else "pt"

        base_key = self._resumes.best_key_for_language(analysis.get("resume_key"), target_lang)
        if not base_key:
            raise RuntimeError("Nenhum currículo-base encontrado em resumes/")

        base_text = self._resumes.text_for(base_key)
        all_summary = self._resumes.as_prompt_block(max_chars_per_resume=2500)
        lang_label = "Inglês" if target_lang == "en" else "Português"

        prompt = f"{TAILORED_RESUME_SYSTEM}\n\n" + TAILORED_RESUME_USER.format(
            language=target_lang,
            language_label=lang_label,
            base_resume_key=base_key,
            base_resume_text=base_text[:7000],
            all_resumes_summary=all_summary[:9000],
            profile_facts=self._profile.as_verified_facts_text()[:3000],
            company=job.get("company") or "—",
            title=job.get("title") or "—",
            location=job.get("location") or "—",
            modality=job.get("modality") or ("Remoto" if job.get("remote") else "—"),
            body=(job.get("body") or "")[:6500],
            matched_skills=", ".join(analysis.get("matched_skills") or []),
            resume_changes="; ".join(analysis.get("resume_changes") or []),
        )

        raw = agy_ask(prompt, json_schema=RESUME_JSON_SCHEMA)
        data = extract_json_object(raw)
        if not data:
            raise RuntimeError("IA não retornou JSON válido para o currículo personalizado")

        # Preserve verified contact details
        if self._profile.full_name:
            data["full_name"] = self._profile.full_name
        verified_contact = build_contact_line(self._profile)
        if verified_contact and not data.get("contact_line"):
            data["contact_line"] = verified_contact

        ok, warnings = validate_tailored_resume(
            data,
            verified_years=self._resumes.all_verified_years(),
            expected_name=self._profile.full_name,
        )
        if not ok:
            raise RuntimeError(f"Validação de veracidade do currículo falhou: {'; '.join(warnings)}")

        md = resume_dict_to_markdown(data, language=target_lang)
        md_path = self._storage.resume_md_path(job_id)
        pdf_path = self._storage.resume_pdf_path(
            job_id,
            candidate_name=self._profile.full_name,
            language=target_lang,
        )
        md_path.write_text(md, encoding="utf-8")

        render_resume_pdf(data, pdf_path, language=target_lang)
        expected_terms = [self._profile.first_name] if self._profile.first_name else []
        pdf_ok, pdf_msg = validate_pdf(pdf_path, expected_terms=expected_terms, min_chars=250, max_pages=3)
        if not pdf_ok:
            raise RuntimeError(f"Validação do PDF do currículo falhou: {pdf_msg}")

        self._storage.write_metadata(
            job_id,
            {
                "job_id": job_id,
                "company": job.get("company") or "",
                "title": job.get("title") or "",
                "url": job.get("url") or "",
                "language": target_lang,
                "base_resume_key": base_key,
                "resume_md": str(md_path),
                "resume_pdf": str(pdf_path),
                "resume_validation": pdf_msg,
                "resume_warnings": warnings,
            },
        )
        logger.info(
            "tailored resume generated",
            extra={"job_id": job_id, "base_key": base_key, "language": target_lang, "pdf": pdf_msg},
        )
        return GeneratedResume(
            job_id=job_id,
            base_resume_key=base_key,
            language=target_lang,
            md_path=md_path,
            pdf_path=pdf_path,
            markdown=md,
            data=data,
            warnings=warnings,
        )
