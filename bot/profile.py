"""Resume library and consolidated verified candidate profile."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

logger = logging.getLogger("jobbot.profile")

_TAIL_CUES = (
    "competências técnicas",
    "technical skills",
    "habilidades",
    "formação",
    "education",
    "idiomas",
    "certifica",
    "referências",
    "informações complementares",
)

_TEXT_SPACING = re.compile(r"[ \t]+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d{1,3}[\s.-]?\(?\d{2}\)?[\s.-]?\d{4,5}[\s.-]?\d{4}")
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9_.-]+/?", re.IGNORECASE)
_GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9_.-]+/?", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s|,)>\]]+", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

_SECTION_HEADING_RE = re.compile(
    r"^(RESUMO|PROFESSIONAL SUMMARY|SUMMARY|EXPERIÊNCIA|EXPERIENCE|"
    r"FORMAÇÃO|EDUCATION|COMPETÊNCIAS|HABILIDADES|SKILLS|TECHNICAL SKILLS|"
    r"PROJETOS|PROJECTS|IDIOMAS|LANGUAGES|CERTIFICAÇÕES|CERTIFICATIONS|"
    r"INFORMAÇÕES|ADDITIONAL)",
    re.IGNORECASE,
)


def normalize_pdf_text(raw: str) -> str:
    """Repair pypdf's justified-text fragmentation where single words sit on separate lines."""
    if not raw:
        return ""
    lines = [ln.strip() for ln in raw.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append(_TEXT_SPACING.sub(" ", " ".join(buf)).strip())
            buf.clear()

    for ln in lines:
        if not ln:
            # Blank lines in justified pypdf output often sit between single words;
            # keep paragraph break only if buffer already looks like a complete line/sentence.
            if buf and (buf[-1].endswith((".", ":", ";")) or len(" ".join(buf)) > 80):
                flush()
                out.append("")
            continue
        is_bullet = ln.startswith(("●", "•", "-", "*", "▪"))
        is_heading = bool(_SECTION_HEADING_RE.match(ln)) and len(ln) <= 60
        if is_bullet or is_heading:
            flush()
            if is_heading and out and out[-1] != "":
                out.append("")
            buf.append(ln)
            if is_heading:
                flush()
                out.append("")
            continue
        # Preserve header lines (name / headline / contact line with |)
        if "|" in ln and len(ln) <= 140 and len(out) < 6:
            flush()
            out.append(_TEXT_SPACING.sub(" ", ln))
            continue
        buf.append(ln)

    flush()
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _cue_pattern(cue: str) -> "re.Pattern[str]":
    spaced = r"[\s]*".join(re.escape(word) for word in cue.split())
    return re.compile(spaced, re.IGNORECASE)


def _smart_truncate(text: str, max_chars: int) -> str:
    """Truncate keeping head + tail sections (skills/education) that matter for fit analysis."""
    if len(text) <= max_chars:
        return text
    for cue in _TAIL_CUES:
        pat = _cue_pattern(cue)
        for m in pat.finditer(text):
            idx = m.start()
            if idx <= 0:
                continue
            line_break = text.rfind("\n", 0, idx)
            if text[line_break + 1 : idx].strip():
                continue
            prev_break = text.rfind("\n", 0, line_break)
            if prev_break != -1 and text[prev_break + 1 : line_break].strip():
                continue
            tail = text[idx:].strip()
            if len(tail) <= max_chars:
                head_budget = max_chars - len(tail) - 10
                if head_budget > 400:
                    return text[:head_budget].rstrip() + "\n...\n\n" + tail
                return tail
    return text[:max_chars]


def smart_truncate(text: str, max_chars: int) -> str:
    """Public helper to truncate text while preserving tail sections (skills/education)."""
    return _smart_truncate(text, max_chars)


def infer_resume_language(key: str, text: str = "") -> str:
    """Infer whether a resume key/text is in Portuguese ('pt') or English ('en')."""
    kl = (key or "").lower()
    if any(tok in kl for tok in ("curriculo", "currículo", "_pt", "-pt", "ptbr", "pt_br")):
        return "pt"
    if any(tok in kl for tok in ("resume", "_en", "-en", "english")):
        return "en"
    sample = (text or "")[:1500].lower()
    pt_hits = sum(1 for w in ("experiência", "formação", "resumo profissional", "competências", "desenvolvedor") if w in sample)
    en_hits = sum(1 for w in ("experience", "education", "professional summary", "skills", "developer") if w in sample)
    return "en" if en_hits > pt_hits else "pt"


class ResumeLibrary:
    """Loads resume PDFs from RESUMES_DIR, extracts and normalizes text once, caches it."""

    def __init__(self, directory: str = "resumes"):
        self._dir = Path(directory)
        self._cache: dict[str, str] = {}

    def keys(self) -> list[str]:
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.pdf"))

    @property
    def count(self) -> int:
        return len(self.keys())

    def path_for(self, key: str) -> Path:
        return self._dir / f"{key}.pdf"

    def text_for(self, key: str) -> str:
        if key not in self._cache:
            path = self.path_for(key)
            reader = PdfReader(str(path))
            raw = "\n".join(page.extract_text() or "" for page in reader.pages)
            self._cache[key] = normalize_pdf_text(raw)
        return self._cache[key]

    def language_for(self, key: str) -> str:
        text = self.text_for(key) if self.path_for(key).exists() else ""
        return infer_resume_language(key, text)

    def best_key_for_language(self, preferred_key: str | None, target_lang: str) -> str | None:
        """Select a sibling resume in target_lang when available, otherwise preferred_key."""
        keys = self.keys()
        if not keys:
            return None
        target_lang = (target_lang or "pt").lower()[:2]
        if preferred_key and preferred_key in keys:
            if self.language_for(preferred_key) == target_lang:
                return preferred_key
            # Look for matching track suffix (e.g. fullstack, front_end, ai_ml) in target language
            parts = re.split(r"[_-]+", preferred_key.lower())
            track_tokens = [p for p in parts if p not in ("curriculo", "resume", "cv", "pt", "en", "raul")]
            for k in keys:
                if self.language_for(k) == target_lang and track_tokens:
                    kl = k.lower()
                    if all(t in kl for t in track_tokens):
                        return k
            return preferred_key
        for k in keys:
            if self.language_for(k) == target_lang:
                return k
        return keys[0]

    def all_verified_years(self) -> set[str]:
        """Collect all 4-digit years mentioned across all source resumes for truthfulness checks."""
        years: set[str] = set()
        for key in self.keys():
            years.update(_YEAR_RE.findall(self.text_for(key)))
        return years

    def as_prompt_block(self, max_chars_per_resume: int = 6000) -> str:
        parts = []
        for key in self.keys():
            lang = self.language_for(key)
            parts.append(
                f"### RESUME KEY: {key} (lang={lang})\n"
                f"{_smart_truncate(self.text_for(key), max_chars_per_resume)}"
            )
        return "\n\n".join(parts)

    def collect_profile_summary(self, max_chars_per_resume: int = 6000) -> str:
        return self.as_prompt_block(max_chars_per_resume)


@dataclass
class CandidateProfile:
    """Verified structured facts about the candidate used for documents and form filling."""

    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    city: str = ""
    state: str = ""
    country: str = "Brasil"
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    years_of_experience: str = ""
    seniority: str = ""
    headline: str = ""
    languages: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    skill_experience_years: dict[str, str] = field(default_factory=dict)
    work_authorization: str = ""
    salary_expectation: str = ""
    salary_min: int = 0
    salary_max: int = 0
    salary_currency: str = "BRL"
    has_cnpj: bool | None = None
    cnpj_open_days: int = 0
    availability: str = ""
    availability_days: int | None = None
    currently_employed: bool | None = None
    current_company: str = ""
    area_preference: str = ""
    has_disability: bool | None = None
    race_ethnicity: str = ""
    sexual_orientation: str = ""
    gender_identity: str = ""
    cpf: str = ""
    education: dict | str = ""
    profile_markdown: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def education_summary(self) -> str:
        if isinstance(self.education, dict):
            summary = str(self.education.get("summary") or "").strip()
            if summary:
                return summary
            parts = [
                str(self.education.get("degree") or "").strip(),
                str(self.education.get("school") or "").strip(),
                " – ".join(
                    p
                    for p in (
                        str(self.education.get("start_date") or "").strip(),
                        str(self.education.get("end_date") or "").strip(),
                    )
                    if p
                ),
            ]
            return " | ".join(p for p in parts if p)
        return str(self.education or "").strip()

    @property
    def education_school(self) -> str:
        if isinstance(self.education, dict):
            return str(self.education.get("school") or "").strip()
        return ""

    @property
    def education_degree(self) -> str:
        if isinstance(self.education, dict):
            return str(self.education.get("degree") or "").strip()
        return ""

    def to_dict(self) -> dict:
        return {
            "full_name": self.full_name,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
            "phone": self.phone,
            "location": self.location,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "linkedin_url": self.linkedin_url,
            "github_url": self.github_url,
            "portfolio_url": self.portfolio_url,
            "years_of_experience": self.years_of_experience,
            "seniority": self.seniority,
            "headline": self.headline,
            "languages": self.languages,
            "skills": self.skills,
            "skill_experience_years": self.skill_experience_years,
            "work_authorization": self.work_authorization,
            "salary_expectation": self.salary_expectation,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "salary_currency": self.salary_currency,
            "has_cnpj": self.has_cnpj,
            "cnpj_open_days": self.cnpj_open_days,
            "availability": self.availability,
            "availability_days": self.availability_days,
            "currently_employed": self.currently_employed,
            "current_company": self.current_company,
            "area_preference": self.area_preference,
            "has_disability": self.has_disability,
            "race_ethnicity": self.race_ethnicity,
            "sexual_orientation": self.sexual_orientation,
            "gender_identity": self.gender_identity,
            "cpf": self.cpf,
            "education": self.education,
        }

    def as_verified_facts_text(self) -> str:
        """Render verified candidate facts for prompts (form Q&A, cover letter, resume)."""
        lines = []
        cnpj_text = ""
        if self.has_cnpj is True:
            cnpj_text = f"Sim, CNPJ aberto há mais de {self.cnpj_open_days or 30} dias"
        elif self.has_cnpj is False:
            cnpj_text = "Não"
        employment_text = ""
        if self.currently_employed is False:
            employment_text = "Atualmente desempregado (sem vínculo empregatício atual, disponível para início imediato)"
        elif self.currently_employed is True:
            employment_text = f"Empregado ({self.current_company})" if self.current_company else "Empregado atualmente"
        sal_text = self.salary_expectation
        if not sal_text and (self.salary_min or self.salary_max):
            sal_text = f"{self.salary_currency} {self.salary_min} - {self.salary_max}"
        for label, value in (
            ("Nome completo", self.full_name),
            ("E-mail", self.email),
            ("Telefone", self.phone),
            ("Localização", self.location),
            ("LinkedIn", self.linkedin_url),
            ("GitHub", self.github_url),
            ("Portfólio", self.portfolio_url),
            ("Senioridade", self.seniority),
            ("Anos de experiência", self.years_of_experience),
            ("Idiomas", ", ".join(self.languages) if self.languages else ""),
            ("Formação acadêmica", self.education_summary),
            ("Skills principais", ", ".join(self.skills) if self.skills else ""),
            (
                "Experiência específica por tecnologia",
                ", ".join(f"{k}: {v} ano(s)" for k, v in self.skill_experience_years.items())
                if self.skill_experience_years
                else "",
            ),
            ("Autorização de trabalho", self.work_authorization),
            ("Pretensão salarial (faixa)", sal_text),
            ("Possui CNPJ/Empresa aberta", cnpj_text),
            ("Disponibilidade de início", self.availability),
            ("Status atual de emprego", employment_text),
        ):
            if value:
                lines.append(f"- {label}: {value}")
        if self.profile_markdown:
            lines.append("\n## Perfil Consolidado\n" + self.profile_markdown.strip())
        return "\n".join(lines).strip()

    @classmethod
    def load(
        cls,
        resume_library: ResumeLibrary | None = None,
        profile_json_path: str = "state/profile.json",
        profile_md_path: str = "state/profile.md",
    ) -> "CandidateProfile":
        """Load profile from state/profile.json + state/profile.md, enriched with resume contacts."""
        data: dict = {}
        jp = Path(profile_json_path)
        if jp.exists():
            try:
                data = json.loads(jp.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to parse %s: %s", jp, exc)

        md_text = ""
        mp = Path(profile_md_path)
        if mp.exists():
            try:
                md_text = mp.read_text(encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to read %s: %s", mp, exc)

        extracted = _extract_contacts_from_resumes(resume_library) if resume_library else {}
        merged = {**extracted, **{k: v for k, v in data.items() if v not in (None, "", [])}}

        full_name = str(merged.get("full_name") or "").strip()
        parts = full_name.split() if full_name else []
        first_name = str(merged.get("first_name") or (parts[0] if parts else "")).strip()
        last_name = str(merged.get("last_name") or (" ".join(parts[1:]) if len(parts) > 1 else "")).strip()

        location = str(merged.get("location") or "").strip()
        city = str(merged.get("city") or "").strip()
        state = str(merged.get("state") or "").strip()
        if location and not city and "," in location:
            loc_parts = [p.strip() for p in location.split(",")]
            city = loc_parts[0]
            if len(loc_parts) > 1 and not state:
                state = loc_parts[1]

        return cls(
            full_name=full_name,
            first_name=first_name,
            last_name=last_name,
            email=str(merged.get("email") or "").strip(),
            phone=str(merged.get("phone") or "").strip(),
            location=location,
            city=city,
            state=state,
            country=str(merged.get("country") or "Brasil").strip(),
            linkedin_url=_ensure_https(str(merged.get("linkedin_url") or "")),
            github_url=_ensure_https(str(merged.get("github_url") or "")),
            portfolio_url=_ensure_https(str(merged.get("portfolio_url") or "")),
            years_of_experience=str(merged.get("years_of_experience") or "").strip(),
            seniority=str(merged.get("seniority") or "").strip(),
            headline=str(merged.get("headline") or "").strip(),
            languages=[str(x).strip() for x in (merged.get("languages") or []) if str(x).strip()],
            skills=[str(x).strip() for x in (merged.get("skills") or []) if str(x).strip()],
            skill_experience_years={
                str(k).strip(): str(v).strip()
                for k, v in (merged.get("skill_experience_years") or {}).items()
                if str(k).strip() and str(v).strip()
            }
            if isinstance(merged.get("skill_experience_years"), dict)
            else {},
            work_authorization=str(merged.get("work_authorization") or "").strip(),
            salary_expectation=str(merged.get("salary_expectation") or "").strip(),
            salary_min=int(merged.get("salary_min") or 0),
            salary_max=int(merged.get("salary_max") or 0),
            salary_currency=str(merged.get("salary_currency") or "BRL").strip(),
            has_cnpj=merged.get("has_cnpj") if isinstance(merged.get("has_cnpj"), bool) else None,
            cnpj_open_days=int(merged.get("cnpj_open_days") or 0),
            availability=str(merged.get("availability") or "").strip(),
            availability_days=(
                int(merged["availability_days"])
                if merged.get("availability_days") is not None and str(merged.get("availability_days")).strip() != ""
                else None
            ),
            currently_employed=(
                merged.get("currently_employed")
                if isinstance(merged.get("currently_employed"), bool)
                else None
            ),
            current_company=str(merged.get("current_company") or "").strip(),
            area_preference=str(merged.get("area_preference") or "").strip(),
            has_disability=(
                merged.get("has_disability")
                if isinstance(merged.get("has_disability"), bool)
                else None
            ),
            race_ethnicity=str(merged.get("race_ethnicity") or "").strip(),
            sexual_orientation=str(merged.get("sexual_orientation") or "").strip(),
            gender_identity=str(merged.get("gender_identity") or "").strip(),
            cpf=str(merged.get("cpf") or "").strip(),
            education=merged.get("education") if isinstance(merged.get("education"), (dict, str)) else "",
            profile_markdown=md_text,
            raw=merged,
        )


def _ensure_https(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


def _extract_contacts_from_resumes(resume_library: ResumeLibrary) -> dict:
    """Deterministically extract verified contact details from the resume PDFs."""
    info: dict[str, str] = {}
    for key in resume_library.keys():
        try:
            text = resume_library.text_for(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read resume %s: %s", key, exc)
            continue
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not info.get("full_name") and lines:
            first_line = lines[0]
            if len(first_line.split()) in (2, 3, 4) and not _EMAIL_RE.search(first_line) and "|" not in first_line:
                info["full_name"] = first_line.title() if first_line.isupper() else first_line
        if not info.get("headline") and len(lines) > 1 and "|" in lines[1]:
            info["headline"] = lines[1]

        header = "\n".join(lines[:6])
        if not info.get("email"):
            m = _EMAIL_RE.search(header) or _EMAIL_RE.search(text)
            if m:
                info["email"] = m.group(0)
        if not info.get("phone"):
            m = _PHONE_RE.search(header) or _PHONE_RE.search(text)
            if m:
                info["phone"] = m.group(0).strip()
        if not info.get("linkedin_url"):
            m = _LINKEDIN_RE.search(text)
            if m:
                info["linkedin_url"] = _ensure_https(m.group(0))
        if not info.get("github_url"):
            m = _GITHUB_RE.search(text)
            if m:
                info["github_url"] = _ensure_https(m.group(0))
        if not info.get("location"):
            for ln in lines[:6]:
                if "|" in ln:
                    first_seg = ln.split("|")[0].strip()
                    if "," in first_seg and len(first_seg) <= 50 and "@" not in first_seg:
                        info["location"] = first_seg
                        break
    return info
