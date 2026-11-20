"""One-off: generate consolidated professional profile (Markdown + JSON) from resumes.

Usage: .venv/bin/python -m bot.generate_profile
Writes state/profile.md and state/profile.json. Review them before first run.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from bot.config import Settings, load_env, setup_logging
from bot.gemini.client import agy_ask, extract_json_object
from bot.profile import CandidateProfile, ResumeLibrary

logger = logging.getLogger("jobbot.genprofile")

PROFILE_MD_SYSTEM = """\
Você é um redator técnico. Com base nos currículos fornecidos, produza um
perfil profissional CONSOLIDADO em markdown, em português, com estas seções:

## Identificação
Nome, localização, senioridade estimada, anos de experiência.

## Stack principal
Lista agrupada por área (frontend, backend, dados, devops, IA).

## Experiência profissional
Cada empresa com cargo, período exato, principais atividades e resultados mensuráveis.

## Idiomas
## Formação
## Diferenciais
## Pontos fracos (gaps prováveis para vagas)

REGRAS ABSOLUTAS:
- Use SOMENTE informações presentes nos currículos. NÃO invente nada.
- NÃO altere datas ou períodos profissionais.
- Consolide (dedup) e harmonize as variações entre os currículos.
- Seja específico: inclua números/percentuais somente quando existirem nos currículos.
- Máximo ~600 palavras.

Responda apenas com o markdown.
"""

PROFILE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "full_name": {"type": "string"},
        "email": {"type": "string"},
        "phone": {"type": "string"},
        "location": {"type": "string"},
        "city": {"type": "string"},
        "state": {"type": "string"},
        "country": {"type": "string"},
        "linkedin_url": {"type": "string"},
        "github_url": {"type": "string"},
        "portfolio_url": {"type": "string"},
        "years_of_experience": {"type": "string"},
        "seniority": {"type": "string"},
        "headline": {"type": "string"},
        "languages": {"type": "array", "items": {"type": "string"}},
        "skills": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["full_name", "email", "location", "years_of_experience", "seniority", "languages", "skills"],
}

PROFILE_JSON_PROMPT = """\
Extraia EXCLUSIVAMENTE os fatos comprovados nos currículos abaixo para preencher o JSON estruturado.
Não invente nenhum campo ausente (deixe string vazia "" se não constar nos currículos).

{resumes}
"""


def main() -> int:
    load_env()
    settings = Settings.from_env()
    setup_logging(settings.log_level, settings.log_format)

    lib = ResumeLibrary(settings.resumes_dir)
    if lib.count == 0:
        logger.error("No resumes found in %s — nothing to generate", settings.resumes_dir)
        return 1
    logger.info("Generating consolidated profile from %d resumes: %s", lib.count, ", ".join(lib.keys()))

    resumes_block = lib.as_prompt_block(max_chars_per_resume=7000)

    md_response = agy_ask(
        f"{PROFILE_MD_SYSTEM.strip()}\n\n{resumes_block}\n\nGere o perfil consolidado.",
        timeout=max(settings.agy_timeout_seconds, 600),
    )
    md_path = Path(settings.profile_md_path)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md_response or "", encoding="utf-8")
    logger.info("Written %s (%d chars)", md_path, len(md_response or ""))

    json_raw = agy_ask(
        PROFILE_JSON_PROMPT.format(resumes=resumes_block),
        json_schema=PROFILE_JSON_SCHEMA,
        timeout=settings.agy_timeout_seconds,
    )
    extracted = extract_json_object(json_raw) or {}
    # Preserve existing manual fields like salary_expectation, work_authorization, availability
    existing_json: dict = {}
    json_path = Path(settings.profile_json_path)
    if json_path.exists():
        try:
            existing_json = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing_json = {}

    fallback_profile = CandidateProfile.load(lib, profile_json_path=str(json_path), profile_md_path=str(md_path))
    merged = {
        **fallback_profile.to_dict(),
        **{k: v for k, v in extracted.items() if v not in (None, "", [])},
    }
    for manual_key in (
        "github_url",
        "portfolio_url",
        "work_authorization",
        "salary_expectation",
        "salary_min",
        "salary_max",
        "salary_currency",
        "has_cnpj",
        "cnpj_open_days",
        "availability",
        "availability_days",
        "currently_employed",
        "current_company",
    ):
        if existing_json.get(manual_key) not in (None, ""):
            merged[manual_key] = existing_json[manual_key]

    json_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(json_path, 0o600)
    logger.info("Written %s", json_path)
    print((md_response or "")[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
