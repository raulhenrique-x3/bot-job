"""Per-application document storage under data/applications/{job_id}/."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_NAME_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def sanitize_job_id(job_id: str) -> str:
    """Convert any portal job ID (e.g. 'li:12345') into a safe directory name."""
    cleaned = _UNSAFE_CHARS.sub("_", (job_id or "unknown").strip()).strip("._")
    return cleaned[:100] or "unknown"


def human_document_filename(
    candidate_name: str,
    doc_type: str,
    language: str = "pt",
    ext: str = "pdf",
) -> str:
    """Build a clean, recruiter-friendly ASCII filename (e.g. 'Raul_Henrique_Curriculo.pdf')."""
    lang = "en" if (language or "pt").lower().startswith("en") else "pt"
    norm = unicodedata.normalize("NFKD", (candidate_name or "").strip())
    ascii_name = "".join(ch for ch in norm if not unicodedata.combining(ch))
    words = _NAME_WORD_RE.findall(ascii_name)[:4]
    name_prefix = "_".join(w.capitalize() if w.islower() or w.isupper() else w for w in words)

    if doc_type == "cover_letter":
        doc_label = "Cover_Letter" if lang == "en" else "Carta_Apresentacao"
    else:
        doc_label = "Resume" if lang == "en" else "Curriculo"

    stem = f"{name_prefix}_{doc_label}" if name_prefix else doc_label
    return f"{stem}.{ext.lstrip('.')}"


class ApplicationStorage:
    """Manages isolated artifacts per job application without overwriting prior jobs."""

    def __init__(self, base_dir: str = "data/applications"):
        self._base = Path(base_dir)

    def dir_for(self, job_id: str) -> Path:
        path = self._base / sanitize_job_id(job_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resume_md_path(self, job_id: str) -> Path:
        return self.dir_for(job_id) / "resume.md"

    def resume_pdf_path(
        self,
        job_id: str,
        *,
        candidate_name: str = "",
        language: str = "pt",
    ) -> Path:
        return self.dir_for(job_id) / human_document_filename(
            candidate_name, "resume", language=language, ext="pdf"
        )

    def cover_letter_md_path(self, job_id: str) -> Path:
        return self.dir_for(job_id) / "cover_letter.md"

    def cover_letter_pdf_path(
        self,
        job_id: str,
        *,
        candidate_name: str = "",
        language: str = "pt",
    ) -> Path:
        return self.dir_for(job_id) / human_document_filename(
            candidate_name, "cover_letter", language=language, ext="pdf"
        )

    def metadata_path(self, job_id: str) -> Path:
        return self.dir_for(job_id) / "metadata.json"

    def read_metadata(self, job_id: str) -> dict[str, Any]:
        path = self.metadata_path(job_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def write_metadata(self, job_id: str, updates: dict[str, Any]) -> Path:
        path = self.metadata_path(job_id)
        data = self.read_metadata(job_id)
        data.update(updates)
        data.setdefault("job_id", job_id)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path
