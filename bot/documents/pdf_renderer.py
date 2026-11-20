"""ATS-friendly single-column PDF renderer (ReportLab) and pypdf validator."""

from __future__ import annotations

import html
import logging
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

logger = logging.getLogger("jobbot.documents.pdf")

_ACCENT = colors.HexColor("#1F2937")
_MUTED = colors.HexColor("#4B5563")
_RULE = colors.HexColor("#CBD5E1")
_BODY = colors.HexColor("#111827")

_FONT_REGISTERED = False
_FONT_REGULAR = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
_FONT_ITALIC = "Helvetica-Oblique"

_FONT_CANDIDATES = (
    (
        "DejaVuSans",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "DejaVuSans-Oblique",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    ),
    (
        "LiberationSans",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "LiberationSans-Bold",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "LiberationSans-Italic",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
    ),
)

_SECTION_LABELS = {
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


def _ensure_fonts() -> tuple[str, str, str]:
    global _FONT_REGISTERED, _FONT_REGULAR, _FONT_BOLD, _FONT_ITALIC
    if _FONT_REGISTERED:
        return _FONT_REGULAR, _FONT_BOLD, _FONT_ITALIC

    for reg_name, reg_path, bold_name, bold_path, ital_name, ital_path in _FONT_CANDIDATES:
        if Path(reg_path).exists() and Path(bold_path).exists():
            try:
                pdfmetrics.registerFont(TTFont(reg_name, reg_path))
                pdfmetrics.registerFont(TTFont(bold_name, bold_path))
                if Path(ital_path).exists():
                    pdfmetrics.registerFont(TTFont(ital_name, ital_path))
                    _FONT_ITALIC = ital_name
                else:
                    _FONT_ITALIC = reg_name
                _FONT_REGULAR = reg_name
                _FONT_BOLD = bold_name
                _FONT_REGISTERED = True
                return _FONT_REGULAR, _FONT_BOLD, _FONT_ITALIC
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not register font %s: %s", reg_name, exc)

    _FONT_REGISTERED = True
    return _FONT_REGULAR, _FONT_BOLD, _FONT_ITALIC


def _safe_text(text: str, using_ttf: bool) -> str:
    """Escape XML entities for ReportLab Paragraph and normalize symbols if using Type1."""
    s = (text or "").strip()
    if not using_ttf:
        replacements = {
            "\u2014": "-",
            "\u2013": "-",
            "\u2022": "-",
            "\u25cf": "-",
            "\u2018": "'",
            "\u2019": "'",
            "\u201c": '"',
            "\u201d": '"',
            "\u00a0": " ",
        }
        for src, dst in replacements.items():
            s = s.replace(src, dst)
        s = s.encode("latin-1", errors="ignore").decode("latin-1")
    return html.escape(s)


def _build_styles() -> tuple[dict[str, ParagraphStyle], bool]:
    reg, bold, ital = _ensure_fonts()
    using_ttf = reg != "Helvetica"
    styles = {
        "name": ParagraphStyle(
            "Name",
            fontName=bold,
            fontSize=16,
            leading=19,
            textColor=_ACCENT,
            alignment=TA_LEFT,
            spaceAfter=2,
        ),
        "headline": ParagraphStyle(
            "Headline",
            fontName=bold,
            fontSize=9.5,
            leading=12.5,
            textColor=_MUTED,
            spaceAfter=3,
        ),
        "contact": ParagraphStyle(
            "Contact",
            fontName=reg,
            fontSize=8.5,
            leading=11.5,
            textColor=_MUTED,
            spaceAfter=6,
        ),
        "section": ParagraphStyle(
            "Section",
            fontName=bold,
            fontSize=10,
            leading=13,
            textColor=_ACCENT,
            spaceBefore=9,
            spaceAfter=2,
            keepWithNext=True,
        ),
        "role": ParagraphStyle(
            "Role",
            fontName=bold,
            fontSize=9.5,
            leading=12.5,
            textColor=_BODY,
            spaceBefore=5,
            spaceAfter=1,
            keepWithNext=True,
        ),
        "meta": ParagraphStyle(
            "Meta",
            fontName=ital,
            fontSize=8.5,
            leading=11,
            textColor=_MUTED,
            spaceAfter=2,
            keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "Body",
            fontName=reg,
            fontSize=9.2,
            leading=13,
            textColor=_BODY,
            spaceAfter=3,
        ),
        "bullet": ParagraphStyle(
            "Bullet",
            fontName=reg,
            fontSize=9.0,
            leading=12.5,
            textColor=_BODY,
            leftIndent=10,
            firstLineIndent=-7,
            spaceAfter=2.2,
        ),
        "letter_body": ParagraphStyle(
            "LetterBody",
            fontName=reg,
            fontSize=10,
            leading=14.5,
            textColor=_BODY,
            spaceAfter=8,
        ),
    }
    return styles, using_ttf


def _section_header(title: str, styles: dict[str, ParagraphStyle], using_ttf: bool) -> list:
    return [
        Paragraph(_safe_text(title, using_ttf), styles["section"]),
        HRFlowable(width="100%", thickness=0.6, color=_RULE, spaceBefore=0, spaceAfter=4),
    ]


def render_resume_pdf(resume_data: dict, output_path: Path | str, language: str = "pt") -> Path:
    """Render an ATS-compatible single-column resume PDF with consistent visual identity."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lang = "en" if (language or "pt").lower().startswith("en") else "pt"
    labels = _SECTION_LABELS[lang]
    styles, using_ttf = _build_styles()

    doc = SimpleDocTemplate(
        str(out),
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"{resume_data.get('full_name', 'Resume')} - Resume",
        author=resume_data.get("full_name", ""),
    )

    story: list = []
    full_name = resume_data.get("full_name") or ""
    headline = resume_data.get("headline") or ""
    contact_line = resume_data.get("contact_line") or ""

    if full_name:
        story.append(Paragraph(_safe_text(full_name, using_ttf), styles["name"]))
    if headline:
        story.append(Paragraph(_safe_text(headline, using_ttf), styles["headline"]))
    if contact_line:
        story.append(Paragraph(_safe_text(contact_line, using_ttf), styles["contact"]))

    summary = (resume_data.get("summary") or "").strip()
    if summary:
        story.extend(_section_header(labels["summary"], styles, using_ttf))
        story.append(Paragraph(_safe_text(summary, using_ttf), styles["body"]))

    experiences = resume_data.get("experiences") or []
    if experiences:
        story.extend(_section_header(labels["experience"], styles, using_ttf))
        for exp in experiences:
            role = (exp.get("role") or "").strip()
            company = (exp.get("company") or "").strip()
            location = (exp.get("location") or "").strip()
            period = (exp.get("period") or "").strip()
            header_parts = [p for p in (role, company) if p]
            if header_parts:
                story.append(Paragraph(_safe_text(" | ".join(header_parts), using_ttf), styles["role"]))
            meta_parts = [p for p in (period, location) if p]
            if meta_parts:
                story.append(Paragraph(_safe_text(" | ".join(meta_parts), using_ttf), styles["meta"]))
            for bullet in exp.get("bullets") or []:
                b = str(bullet).strip().lstrip("-•●* ")
                if b:
                    story.append(Paragraph(f"- {_safe_text(b, using_ttf)}", styles["bullet"]))

    skills = resume_data.get("skills") or []
    if skills:
        story.extend(_section_header(labels["skills"], styles, using_ttf))
        for group in skills:
            if isinstance(group, dict):
                cat = (group.get("category") or "").strip()
                items = [str(i).strip() for i in (group.get("items") or []) if str(i).strip()]
                if items:
                    prefix = f"<b>{_safe_text(cat, using_ttf)}:</b> " if cat else ""
                    story.append(Paragraph(prefix + _safe_text(", ".join(items), using_ttf), styles["body"]))
            else:
                text = str(group).strip()
                if text:
                    story.append(Paragraph(_safe_text(text, using_ttf), styles["body"]))

    projects = resume_data.get("projects") or []
    if projects:
        story.extend(_section_header(labels["projects"], styles, using_ttf))
        for proj in projects:
            if isinstance(proj, dict):
                name = (proj.get("name") or "").strip()
                desc = (proj.get("description") or "").strip()
                if name:
                    line = f"<b>{_safe_text(name, using_ttf)}</b>"
                    if desc:
                        line += f": {_safe_text(desc, using_ttf)}"
                    story.append(Paragraph(line, styles["body"]))
            elif str(proj).strip():
                story.append(Paragraph(f"- {_safe_text(str(proj), using_ttf)}", styles["bullet"]))

    education = resume_data.get("education") or []
    if education:
        story.extend(_section_header(labels["education"], styles, using_ttf))
        for edu in education:
            if isinstance(edu, dict):
                degree = (edu.get("degree") or "").strip()
                inst = (edu.get("institution") or "").strip()
                period = (edu.get("period") or "").strip()
                line = " | ".join(p for p in (degree, inst, period) if p)
                if line:
                    story.append(Paragraph(_safe_text(line, using_ttf), styles["body"]))
            elif str(edu).strip():
                story.append(Paragraph(_safe_text(str(edu), using_ttf), styles["body"]))

    certifications = resume_data.get("certifications") or []
    if certifications:
        story.extend(_section_header(labels["certifications"], styles, using_ttf))
        for cert in certifications:
            c = str(cert).strip()
            if c:
                story.append(Paragraph(f"- {_safe_text(c, using_ttf)}", styles["bullet"]))

    languages = resume_data.get("languages") or []
    if languages:
        story.extend(_section_header(labels["languages"], styles, using_ttf))
        clean_langs = [str(x).strip() for x in languages if str(x).strip()]
        if clean_langs:
            story.append(Paragraph(_safe_text(" | ".join(clean_langs), using_ttf), styles["body"]))

    doc.build(story)
    return out


def render_cover_letter_pdf(
    letter_data: dict,
    candidate_header: dict,
    output_path: Path | str,
    language: str = "pt",
) -> Path:
    """Render a matching single-column ATS-friendly cover letter PDF."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    styles, using_ttf = _build_styles()

    doc = SimpleDocTemplate(
        str(out),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"{candidate_header.get('full_name', 'Candidate')} - Cover Letter",
        author=candidate_header.get("full_name", ""),
    )

    story: list = []
    full_name = candidate_header.get("full_name") or ""
    headline = candidate_header.get("headline") or ""
    contact_line = candidate_header.get("contact_line") or ""

    if full_name:
        story.append(Paragraph(_safe_text(full_name, using_ttf), styles["name"]))
    if headline:
        story.append(Paragraph(_safe_text(headline, using_ttf), styles["headline"]))
    if contact_line:
        story.append(Paragraph(_safe_text(contact_line, using_ttf), styles["contact"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=_RULE, spaceBefore=2, spaceAfter=12))

    date_str = (letter_data.get("date") or "").strip()
    if date_str:
        story.append(Paragraph(_safe_text(date_str, using_ttf), styles["meta"]))
        story.append(Spacer(1, 4))

    recipient = (letter_data.get("recipient_line") or "").strip()
    if recipient:
        story.append(Paragraph(_safe_text(recipient, using_ttf), styles["body"]))
        story.append(Spacer(1, 6))

    salutation = (letter_data.get("salutation") or "").strip()
    if salutation:
        story.append(Paragraph(_safe_text(salutation, using_ttf), styles["letter_body"]))

    for para in letter_data.get("paragraphs") or []:
        text = str(para).strip()
        if text:
            story.append(Paragraph(_safe_text(text, using_ttf), styles["letter_body"]))

    closing = (letter_data.get("closing") or "").strip()
    signature = (letter_data.get("signature") or full_name).strip()
    if closing:
        story.append(Spacer(1, 4))
        story.append(Paragraph(_safe_text(closing, using_ttf), styles["letter_body"]))
    if signature:
        story.append(Paragraph(f"<b>{_safe_text(signature, using_ttf)}</b>", styles["letter_body"]))

    doc.build(story)
    return out


def validate_pdf(
    pdf_path: Path | str,
    *,
    expected_terms: list[str] | None = None,
    min_chars: int = 180,
    max_pages: int = 3,
) -> tuple[bool, str]:
    """Validate that a generated PDF exists, is readable by pypdf, and contains expected text."""
    path = Path(pdf_path)
    if not path.exists():
        return False, f"PDF não encontrado: {path}"
    if path.stat().st_size < 500:
        return False, f"PDF muito pequeno ({path.stat().st_size} bytes)"
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        return False, f"Falha ao ler PDF com pypdf: {exc}"

    num_pages = len(reader.pages)
    if num_pages < 1 or num_pages > max_pages:
        return False, f"Número de páginas inválido: {num_pages} (esperado 1..{max_pages})"

    extracted = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    if len(extracted) < min_chars:
        return False, f"Texto extraído insuficiente ({len(extracted)} chars < {min_chars})"

    lowered = extracted.lower()
    for term in expected_terms or []:
        t = (term or "").strip().lower()
        if t and t not in lowered:
            return False, f"Termo obrigatório ausente no PDF: {term!r}"

    return True, f"ok ({num_pages}p, {len(extracted)} chars)"
