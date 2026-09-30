"""One-off: generate consolidated professional profile from the 4 resumes.

Usage: .venv/bin/python -m bot.generate_profile
Writes state/profile.md. Review it before first run.
"""

import logging
import os
import sys

from bot.config import setup_logging
from bot.gemini.analysis import agy_ask
from bot.profile import ResumeLibrary

logger = logging.getLogger("jobbot.genprofile")

SYSTEM = """\
Você é um redator técnico. Com base nos 4 currículos fornecidos, produza um
perfil profissional CONSOLIDADO em markdown, em português, com estas seções:

## Identificação
Nome, senioridade estimada, anos de experiência.

## Stack principal
Lista agrupada por área (frontend, backend, dados, devops, IA).

## Experiência profissional
Cada empresa com cargo, período, principais atividades e resultados mensuráveis.

## Idiomas
## Formação
## Diferenciais
## Pontos fracos (gaps prováveis para vagas)

REGRAS ABSOLUTAS:
- Use SOMENTE informações presentes nos currículos. NÃO invente nada.
- Consolide (dedup) e harmonize as variações entre os 4 currículos.
- Seja específico: inclua números/percentuais quando existirem.
- Máximo ~500 palavras.

Responda apenas com o markdown.
"""

USER = "Currículos:\n\n{resumes}\n\nGere o perfil consolidado."


def main() -> int:
    setup_logging("INFO")
    lib = ResumeLibrary("resumes")

    response = agy_ask(USER.format(resumes=lib.as_prompt_block(max_chars_per_resume=5000)), timeout=600)
    os.makedirs("state", exist_ok=True)
    with open("state/profile.md", "w") as fh:
        fh.write(response or "")
    logger.info("written state/profile.md (%d chars)", len(response or ""))
    print((response or "")[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
