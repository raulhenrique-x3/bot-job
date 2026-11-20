"""Tests for tailored ATS resume generation, truthfulness validation, cover letters, and PDF checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.documents import (
    ApplicationStorage,
    CoverLetterGenerator,
    TailoredResumeGenerator,
    human_document_filename,
    sanitize_job_id,
    validate_pdf,
)
from bot.profile import CandidateProfile, ResumeLibrary
from tests.conftest import SAMPLE_RESUME_EN, SAMPLE_RESUME_PT


def test_storage_isolates_per_job_and_sanitizes_ids(tmp_path: Path):
    assert sanitize_job_id("li:123456") == "li_123456"
    assert sanitize_job_id("../../etc/passwd") == "etc_passwd"
    assert human_document_filename("Raul Henrique", "resume", "pt") == "Raul_Henrique_Curriculo.pdf"
    assert human_document_filename("Raul Henrique", "resume", "en") == "Raul_Henrique_Resume.pdf"
    assert human_document_filename("Raul Henrique", "cover_letter", "pt") == "Raul_Henrique_Carta_Apresentacao.pdf"
    assert human_document_filename("Raul Henrique", "cover_letter", "en") == "Raul_Henrique_Cover_Letter.pdf"
    assert human_document_filename("João Paulo Araújo", "resume", "pt") == "Joao_Paulo_Araujo_Curriculo.pdf"

    storage = ApplicationStorage(str(tmp_path / "applications"))
    storage.write_metadata("li:123", {"company": "Acme", "status": "documents_ready"})
    storage.write_metadata("li:456", {"company": "Beta", "status": "applied"})

    assert storage.read_metadata("li:123")["company"] == "Acme"
    assert storage.read_metadata("li:456")["company"] == "Beta"
    assert storage.dir_for("li:123") != storage.dir_for("li:456")


def test_tailored_resume_generator_pt_and_en_with_pdf_validation(
    monkeypatch, tmp_path: Path, resume_library: ResumeLibrary, sample_profile: CandidateProfile
):
    storage = ApplicationStorage(str(tmp_path / "applications"))
    gen = TailoredResumeGenerator(resume_library, sample_profile, storage)

    # English job -> English ATS PDF
    monkeypatch.setattr("bot.documents.resume_generator.agy_ask", lambda prompt, **kw: json.dumps(SAMPLE_RESUME_EN))
    res_en = gen.generate(
        {"id": "gh:701", "company": "GlobalTech", "title": "Fullstack Engineer", "url": "https://example.com/701", "body": "Python React"},
        {"language": "en", "resume_key": "resume_raul_fullstack", "matched_skills": ["Python", "React"], "resume_changes": []},
    )
    assert res_en.language == "en"
    assert res_en.pdf_path.exists()
    assert res_en.pdf_path.name == "Raul_Henrique_Resume.pdf"
    assert res_en.md_path.exists()
    assert "PROFESSIONAL EXPERIENCE" in res_en.markdown
    ok, msg = validate_pdf(res_en.pdf_path, expected_terms=["Raul", "PROFESSIONAL EXPERIENCE"])
    assert ok, msg

    # Portuguese job -> Portuguese ATS PDF
    monkeypatch.setattr("bot.documents.resume_generator.agy_ask", lambda prompt, **kw: json.dumps(SAMPLE_RESUME_PT))
    res_pt = gen.generate(
        {"id": "pt:702", "company": "EmpresaBR", "title": "Desenvolvedor Fullstack", "url": "https://example.com/702", "body": "Python React"},
        {"language": "pt", "resume_key": "curriculo_raul_fullstack", "matched_skills": ["Python", "React"], "resume_changes": []},
    )
    assert res_pt.language == "pt"
    assert res_pt.pdf_path.name == "Raul_Henrique_Curriculo.pdf"
    assert "EXPERIÊNCIA PROFISSIONAL" in res_pt.markdown
    ok_pt, msg_pt = validate_pdf(res_pt.pdf_path, expected_terms=["Raul", "BSATech"])
    assert ok_pt, msg_pt


def test_tailored_resume_rejects_hallucinated_years(
    monkeypatch, tmp_path: Path, resume_library: ResumeLibrary, sample_profile: CandidateProfile
):
    storage = ApplicationStorage(str(tmp_path / "applications"))
    gen = TailoredResumeGenerator(resume_library, sample_profile, storage)

    hallucinated = json.loads(json.dumps(SAMPLE_RESUME_PT))
    hallucinated["experiences"][0]["period"] = "2015 - 2025"  # 2015 is NOT in source resumes!
    monkeypatch.setattr("bot.documents.resume_generator.agy_ask", lambda prompt, **kw: json.dumps(hallucinated))

    with pytest.raises(RuntimeError, match="2015"):
        gen.generate(
            {"id": "gh:bad", "company": "X", "title": "Dev", "url": "https://example.com", "body": "Python"},
            {"language": "pt", "resume_key": "curriculo_raul_fullstack"},
        )


def test_cover_letter_generator_creates_validated_pdf(
    monkeypatch, tmp_path: Path, sample_profile: CandidateProfile
):
    storage = ApplicationStorage(str(tmp_path / "applications"))
    cl_gen = CoverLetterGenerator(sample_profile, storage)

    letter_payload = {
        "recipient_line": "Hiring Team — GlobalTech",
        "salutation": "Dear Hiring Team,",
        "paragraphs": [
            "I am applying for the Fullstack Engineer role at GlobalTech. Over the past four years at BSATech, I have built web applications using React, Next.js, TypeScript, Node.js, and FastAPI.",
            "In my recent work, I refactored core frontend workflows reducing load times by approximately 30% and built REST microservices maintaining average latency under 200 ms.",
            "I would welcome the opportunity to contribute this end-to-end engineering experience to your product team.",
        ],
        "closing": "Best regards,",
        "signature": "Raul Henrique",
    }
    monkeypatch.setattr("bot.documents.cover_letter.agy_ask", lambda prompt, **kw: json.dumps(letter_payload))

    cl = cl_gen.generate(
        {"id": "gh:800", "company": "GlobalTech", "title": "Fullstack Engineer", "body": "React Python"},
        "# Raul Henrique\nFullstack Developer...",
        language="en",
    )
    assert cl.pdf_path.exists()
    assert cl.pdf_path.name == "Raul_Henrique_Cover_Letter.pdf"
    assert cl.md_path.exists()
    ok, msg = validate_pdf(cl.pdf_path, expected_terms=["Raul", "GlobalTech"], max_pages=2)
    assert ok, msg
