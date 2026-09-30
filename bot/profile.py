import logging
from pathlib import Path

from pypdf import PdfReader

logger = logging.getLogger("jobbot.profile")


class ResumeLibrary:
    """Loads resume PDFs from RESUMES_DIR, extracts text once, caches it."""

    def __init__(self, directory: str = "resumes"):
        self._dir = Path(directory)
        self._cache: dict[str, str] = {}

    def keys(self) -> list[str]:
        return sorted(p.stem for p in self._dir.glob("*.pdf"))

    def path_for(self, key: str) -> Path:
        return self._dir / f"{key}.pdf"

    def text_for(self, key: str) -> str:
        if key not in self._cache:
            path = self.path_for(key)
            reader = PdfReader(str(path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            self._cache[key] = text[:6000]
        return self._cache[key]

    def as_prompt_block(self, max_chars_per_resume: int = 3500) -> str:
        parts = []
        for key in self.keys():
            parts.append(f"### RESUME KEY: {key}\n{self.text_for(key)[:max_chars_per_resume]}")
        return "\n\n".join(parts)

    def collect_profile_summary(self, max_chars_per_resume: int = 3500) -> str:
        return self.as_prompt_block(max_chars_per_resume)
