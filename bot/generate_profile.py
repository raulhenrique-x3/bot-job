"""One-off: generate consolidated professional profile from the resumes.

Usage: .venv/bin/python -m bot.generate_profile
Writes state/profile.md. Review it before first run.
"""

import logging
import os
import shutil
import subprocess
import sys

from bot.config import setup_logging
from bot.profile import ResumeLibrary

logger = logging.getLogger("jobbot.genprofile")

SYSTEM = """\
Você é um redator técnico. Com base nos currículos fornecidos, produza um
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
- Consolide (dedup) e harmonize as variações entre os currículos.
- Seja específico: inclua números/percentuais quando existirem.
- Máximo ~500 palavras.

Responda apenas com o markdown.
"""

USER = "{instruction}\n\n{resumes}\n\nGere o perfil consolidado"
INSTRUCTION = SYSTEM.strip()


def agy_markdown(prompt: str, timeout: int = 600) -> str:
    """Call agy without the JSON job-analysis schema — plain markdown output."""
    binary = shutil.which("agy")
    if not binary:
        raise RuntimeError("agy not found in PATH (~/.local/bin/agy)")
    model = os.environ.get("AGY_MODEL")
    if model:
        logger.info("using model override %s", model)
        invocations = ["--model", model]
    else:
        invocations = []
    resp = subprocess.run(
        [binary, "-p", prompt, "--output-format", "text", *invocations],
        capture_output=True, text=True, timeout=timeout,
    )
    if resp.returncode != 0:
        raise RuntimeError(resp.stderr.strip() or f"agy exit {resp.returncode}")
    return resp.stdout.strip()


def main() -> int:
    setup_logging("INFO")
    lib = ResumeLibrary("resumes")
    if lib.count == 0:
        logger.error("no resumes found in resumes/ — nothing to generate")
        return 1
    logger.info("generating profile from %d resumes: %s", lib.count, ", ".join(lib.keys()))

    response = agy_markdown(
        USER.format(instruction=INSTRUCTION, resumes=lib.as_prompt_block(max_chars_per_resume=6000))
    )
    os.makedirs("state", exist_ok=True)
    with open("state/profile.md", "w") as fh:
        fh.write(response or "")
    logger.info("written state/profile.md (%d chars)", len(response or ""))
    print((response or "")[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
