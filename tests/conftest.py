"""Shared pytest fixtures for bot-job unit and integration tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.config import Settings
from bot.database.db import Database
from bot.documents.pdf_renderer import render_resume_pdf
from bot.profile import CandidateProfile, ResumeLibrary

SAMPLE_RESUME_PT = {
    "full_name": "Raul Henrique",
    "headline": "Desenvolvedor Fullstack | Python • TypeScript • React • Node.js",
    "contact_line": "Recife, PE | +55 81 99999-0000 | raul@example.com | linkedin.com/in/raul-henrique",
    "summary": "Engenheiro de Software com mais de 4 anos de experiência em Python, TypeScript, React e Node.js.",
    "experiences": [
        {
            "company": "BSATech",
            "role": "Desenvolvedor Fullstack",
            "location": "Recife, PE",
            "period": "2022 - 2025",
            "bullets": [
                "Desenvolvimento de aplicações React, Next.js e TypeScript reduzindo tempo de carregamento em ~30%.",
                "APIs REST com Node.js, FastAPI, SQL Server e MongoDB com resposta média abaixo de 200 ms.",
            ],
        }
    ],
    "skills": [
        {"category": "Backend", "items": ["Python", "FastAPI", "Node.js", "NestJS", "SQL"]},
        {"category": "Frontend", "items": ["React", "Next.js", "TypeScript"]},
    ],
    "education": [
        {"degree": "Análise e Desenvolvimento de Sistemas", "institution": "Universidade Exemplo", "period": "2020 - 2023"}
    ],
    "languages": ["Português (Nativo)", "Inglês (Avançado)"],
}

SAMPLE_RESUME_EN = {
    "full_name": "Raul Henrique",
    "headline": "Fullstack Developer | Python • TypeScript • React • Node.js",
    "contact_line": "Recife, PE | +55 81 99999-0000 | raul@example.com | linkedin.com/in/raul-henrique",
    "summary": "Software Engineer with 4+ years of experience building web applications with Python, TypeScript, React, and Node.js.",
    "experiences": [
        {
            "company": "BSATech",
            "role": "Fullstack Developer",
            "location": "Recife, PE",
            "period": "2022 - 2025",
            "bullets": [
                "Built modular React, Next.js, and TypeScript applications reducing load times by ~30%.",
                "Engineered REST APIs with Node.js, FastAPI, SQL Server, and MongoDB averaging under 200 ms latency.",
            ],
        }
    ],
    "skills": [
        {"category": "Backend", "items": ["Python", "FastAPI", "Node.js", "NestJS", "SQL"]},
        {"category": "Frontend", "items": ["React", "Next.js", "TypeScript"]},
    ],
    "education": [
        {"degree": "Systems Analysis and Development", "institution": "Example University", "period": "2020 - 2023"}
    ],
    "languages": ["Portuguese (Native)", "English (Advanced)"],
}


@pytest.fixture
def tmp_resumes(tmp_path: Path) -> Path:
    rdir = tmp_path / "resumes"
    rdir.mkdir()
    render_resume_pdf(SAMPLE_RESUME_PT, rdir / "curriculo_raul_fullstack.pdf", language="pt")
    render_resume_pdf(SAMPLE_RESUME_EN, rdir / "resume_raul_fullstack.pdf", language="en")
    return rdir


@pytest.fixture
def resume_library(tmp_resumes: Path) -> ResumeLibrary:
    return ResumeLibrary(str(tmp_resumes))


@pytest.fixture
def sample_profile(tmp_path: Path, resume_library: ResumeLibrary) -> CandidateProfile:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    profile_json = state_dir / "profile.json"
    profile_md = state_dir / "profile.md"
    profile_json.write_text(
        json.dumps(
            {
                "full_name": "Raul Henrique",
                "email": "raul@example.com",
                "phone": "+55 81 99999-0000",
                "location": "Recife, PE",
                "city": "Recife",
                "state": "PE",
                "country": "Brasil",
                "linkedin_url": "https://linkedin.com/in/raul-henrique",
                "github_url": "https://github.com/raulhenrique-x3",
                "years_of_experience": "4+",
                "seniority": "Pleno",
                "languages": ["Português (Nativo)", "Inglês (Avançado)"],
                "skills": ["Python", "TypeScript", "React", "Node.js", "FastAPI", "Docker"],
            }
        ),
        encoding="utf-8",
    )
    profile_md.write_text("## Identificação\nRaul Henrique — Desenvolvedor Fullstack (4+ anos).\n", encoding="utf-8")
    return CandidateProfile.load(resume_library, str(profile_json), str(profile_md))


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "jobs.db"))
    yield db
    db.close()


@pytest.fixture
def test_settings(tmp_path: Path, tmp_resumes: Path) -> Settings:
    return Settings(
        agy_model="gemini-4-neon-medium",
        agy_reasoning_effort="medium",
        job_match_threshold=75,
        max_analyses_per_run=5,
        auto_apply_enabled=True,
        application_dry_run=False,
        max_applications_per_run=5,
        max_applications_per_day=20,
        application_interval_seconds=0,
        application_max_retries=2,
        application_retry_backoff_minutes=1,
        resumes_dir=str(tmp_resumes),
        applications_dir=str(tmp_path / "applications"),
        database_path=str(tmp_path / "jobs.db"),
        profile_json_path=str(tmp_path / "state" / "profile.json"),
        profile_md_path=str(tmp_path / "state" / "profile.md"),
        lock_file=str(tmp_path / "state" / "jobbot.lock"),
        log_format="json",
    )
