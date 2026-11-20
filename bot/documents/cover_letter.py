"""On-demand personalized cover letter generator (only invoked when platform requests one)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot.documents.pdf_renderer import render_cover_letter_pdf, validate_pdf
from bot.documents.resume_generator import build_contact_line
from bot.documents.storage import ApplicationStorage
from bot.gemini.client import agy_ask, extract_json_object
from bot.gemini.prompts import COVER_LETTER_SYSTEM, COVER_LETTER_USER
from bot.profile import CandidateProfile

logger = logging.getLogger("jobbot.documents.cover_letter")

COVER_LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "recipient_line": {"type": "string"},
        "salutation": {"type": "string"},
        "paragraphs": {"type": "array", "items": {"type": "string"}},
        "closing": {"type": "string"},
        "signature": {"type": "string"},
    },
    "required": ["salutation", "paragraphs", "closing", "signature"],
}


@dataclass
class GeneratedCoverLetter:
    job_id: str
    language: str
    md_path: Path
    pdf_path: Path
    plain_text: str
    markdown: str


def _format_date(language: str) -> str:
    now = datetime.now(timezone.utc)
    if language == "en":
        return now.strftime("%B %d, %Y")
    months_pt = (
        "janeiro", "fevereiro", "março", "abril", "maio", "junho",
        "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
    )
    return f"{now.day} de {months_pt[now.month - 1]} de {now.year}"


def cover_letter_to_text(letter_data: dict) -> str:
    parts: list[str] = []
    if letter_data.get("salutation"):
        parts.append(letter_data["salutation"].strip())
    for p in letter_data.get("paragraphs") or []:
        t = str(p).strip()
        if t:
            parts.append(t)
    closing = (letter_data.get("closing") or "").strip()
    signature = (letter_data.get("signature") or "").strip()
    if closing or signature:
        parts.append("\n".join(x for x in (closing, signature) if x))
    return "\n\n".join(parts).strip()


def cover_letter_to_markdown(letter_data: dict, candidate_header: dict) -> str:
    lines: list[str] = []
    if candidate_header.get("full_name"):
        lines.append(f"# {candidate_header['full_name']}")
    if candidate_header.get("contact_line"):
        lines.append(candidate_header["contact_line"])
    lines.append("")
    if letter_data.get("date"):
        lines.append(f"*{letter_data['date']}*")
        lines.append("")
    if letter_data.get("recipient_line"):
        lines.append(letter_data["recipient_line"])
        lines.append("")
    lines.append(cover_letter_to_text(letter_data))
    return "\n".join(lines).strip() + "\n"


class CoverLetterGenerator:
    """Generates tailored cover_letter.md + cover_letter.pdf ONLY when requested."""

    def __init__(self, profile: CandidateProfile, storage: ApplicationStorage):
        self._profile = profile
        self._storage = storage

    def generate(
        self,
        job: dict,
        tailored_resume_md: str,
        *,
        language: str = "pt",
        candidate_headline: str = "",
    ) -> GeneratedCoverLetter:
        job_id = job["id"]
        target_lang = "en" if (language or "pt").lower().startswith("en") else "pt"
        lang_label = "Inglês" if target_lang == "en" else "Português"

        prompt = f"{COVER_LETTER_SYSTEM}\n\n" + COVER_LETTER_USER.format(
            language=target_lang,
            language_label=lang_label,
            profile_facts=self._profile.as_verified_facts_text()[:3000],
            tailored_resume_md=tailored_resume_md[:5000],
            company=job.get("company") or "—",
            title=job.get("title") or "—",
            body=(job.get("body") or "")[:5500],
        )

        raw = agy_ask(prompt, json_schema=COVER_LETTER_SCHEMA)
        data = extract_json_object(raw)
        if not data or not data.get("paragraphs"):
            raise RuntimeError("IA não retornou JSON válido para a carta de apresentação")

        data["date"] = _format_date(target_lang)
        if self._profile.full_name:
            data["signature"] = self._profile.full_name

        header = {
            "full_name": self._profile.full_name,
            "headline": candidate_headline or self._profile.headline,
            "contact_line": build_contact_line(self._profile),
        }

        plain = cover_letter_to_text(data)
        md = cover_letter_to_markdown(data, header)
        md_path = self._storage.cover_letter_md_path(job_id)
        pdf_path = self._storage.cover_letter_pdf_path(
            job_id,
            candidate_name=self._profile.full_name,
            language=target_lang,
        )
        md_path.write_text(md, encoding="utf-8")

        render_cover_letter_pdf(data, header, pdf_path, language=target_lang)
        expected_terms = [self._profile.first_name] if self._profile.first_name else []
        ok, msg = validate_pdf(pdf_path, expected_terms=expected_terms, min_chars=180, max_pages=2)
        if not ok:
            raise RuntimeError(f"Validação do PDF da carta de apresentação falhou: {msg}")

        self._storage.write_metadata(
            job_id,
            {
                "cover_letter_md": str(md_path),
                "cover_letter_pdf": str(pdf_path),
                "cover_letter_validation": msg,
            },
        )
        logger.info("cover letter generated", extra={"job_id": job_id, "language": target_lang, "pdf": msg})
        return GeneratedCoverLetter(
            job_id=job_id,
            language=target_lang,
            md_path=md_path,
            pdf_path=pdf_path,
            plain_text=plain,
            markdown=md,
        )
