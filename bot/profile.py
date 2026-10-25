import logging
import re
from pathlib import Path

from pypdf import PdfReader

logger = logging.getLogger("jobbot.profile")

# Indicadores de seções finais do currículo (skills/formação) que importam
# para análise de fit; extração de PDF colapsa espaços entre palavras, então
# os espaços são tolerados na busca.
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


def _cue_pattern(cue: str) -> "re.Pattern[str]":
    spaced = r"[\s]*".join(re.escape(word) for word in cue.split())
    return re.compile(spaced, re.IGNORECASE)


def _smart_truncate(text: str, max_chars: int) -> str:
    """Truncate keeping tail sections (skills/education) that matter for fit analysis."""
    if len(text) <= max_chars:
        return text
    for cue in _TAIL_CUES:
        pat = _cue_pattern(cue)
        for m in pat.finditer(text):
            idx = m.start()
            if idx <= 0:
                continue
            line_break = text.rfind("\n", 0, idx)
            # Cue precisa começar uma linha (heading): nada além de espaços
            # entre a quebra de linha anterior e o início do cue.
            if text[line_break + 1:idx].strip():
                continue
            # E a linha de cima deve estar "em branco" — a extração de PDF
            # colapsa linhas em " \n ", então espaço isolado conta como vazia.
            prev_break = text.rfind("\n", 0, line_break)
            if prev_break != -1 and text[prev_break + 1:line_break].strip():
                continue
            if len(text) - idx <= max_chars:
                return text[idx:].strip()
    return text[-max_chars:]


class ResumeLibrary:
    """Loads resume PDFs from RESUMES_DIR, extracts text once, caches it."""

    def __init__(self, directory: str = "resumes"):
        self._dir = Path(directory)
        self._cache: dict[str, str] = {}

    def keys(self) -> list[str]:
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
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            self._cache[key] = _TEXT_SPACING.sub(" ", text)
        return self._cache[key]

    def as_prompt_block(self, max_chars_per_resume: int = 6000) -> str:
        parts = []
        for key in self.keys():
            parts.append(f"### RESUME KEY: {key}\n{_smart_truncate(self.text_for(key), max_chars_per_resume)}")
        return "\n\n".join(parts)

    def collect_profile_summary(self, max_chars_per_resume: int = 6000) -> str:
        return self.as_prompt_block(max_chars_per_resume)
