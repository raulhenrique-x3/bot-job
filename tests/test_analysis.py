"""Tests for language detection, enhanced job analysis, eliminatory blockers, and resume selection."""

from __future__ import annotations

import json
from pathlib import Path

from bot.filters.filters import detect_language
from bot.gemini.analysis import JobAnalyzer, should_apply
from bot.profile import CandidateProfile, ResumeLibrary, normalize_pdf_text


def test_detect_language_pt_and_en():
    assert detect_language("Desenvolvedor Fullstack Sênior", "Requisitos: experiência com Python, contratação CLT remoto") == "pt"
    assert detect_language("Senior Software Engineer", "Requirements: 4+ years experience, responsibilities, remote full-time benefits") == "en"
    assert detect_language("SWE", "", default="en") == "en"


def test_normalize_pdf_text_repairs_justified_lines():
    fragmented = "RAUL HENRIQUE\nEngenheiro\n \nde\n \nSoftware\n \ncom\n \nPython.\n\nEXPERIÊNCIA\n● APIs\n \nREST"
    cleaned = normalize_pdf_text(fragmented)
    assert "Engenheiro de Software com Python." in cleaned
    assert "EXPERIÊNCIA" in cleaned
    assert "● APIs REST" in cleaned


def test_should_apply_rejects_eliminatory_blockers_and_low_match():
    ok, reason = should_apply({"match": 90, "recommendation": "apply", "has_eliminatory_blocker": True, "eliminatory_blockers": ["Requer cidadania americana"]}, threshold=75)
    assert ok is False
    assert "eliminatório" in reason.lower()

    ok2, reason2 = should_apply({"match": 68, "recommendation": "maybe", "has_eliminatory_blocker": False, "eliminatory_blockers": []}, threshold=75)
    assert ok2 is False
    assert "68%" in reason2

    ok3, reason3 = should_apply({"match": 84, "recommendation": "apply", "has_eliminatory_blocker": False, "eliminatory_blockers": []}, threshold=75)
    assert ok3 is True
    assert reason3 == ""


def test_job_analyzer_parses_criteria_and_matches_resume_language(monkeypatch, resume_library: ResumeLibrary, sample_profile: CandidateProfile):
    monkeypatch.setattr("bot.gemini.analysis.agy_available", lambda: True)
    ai_payload = {
        "match": 87,
        "recommendation": "apply",
        "language": "en",
        "seniority": "Mid-level",
        "has_eliminatory_blocker": False,
        "eliminatory_blockers": [],
        "matched_skills": ["Python", "TypeScript", "React", "FastAPI"],
        "missing_required_skills": [],
        "missing_skills": ["Kubernetes"],
        "experience_fit": "4+ years matches 3+ years requirement",
        "location_fit": "Remote LATAM compatible",
        "salary_fit": "Not specified",
        "reasons": ["Direct stack overlap", "Production React & FastAPI experience"],
        "resume_key": "curriculo_raul_fullstack",  # PT key -> analyzer should auto-pick EN sibling!
        "resume_changes": ["Highlight FastAPI microservices"],
    }
    monkeypatch.setattr("bot.gemini.analysis.agy_ask", lambda prompt, **kwargs: json.dumps(ai_payload))

    analyzer = JobAnalyzer(resume_library, candidate_profile=sample_profile)
    result = analyzer.analyze({
        "id": "gh:500",
        "source": "greenhouse",
        "title": "Fullstack Engineer (Remote)",
        "company": "GlobalTech",
        "location": "Remote",
        "url": "https://boards.greenhouse.io/globaltech/jobs/500",
        "body": "We are looking for a Fullstack Engineer with Python, FastAPI, React and TypeScript.",
    })
    assert result is not None
    assert result["match"] == 87
    assert result["language"] == "en"
    assert result["resume_key"] == "resume_raul_fullstack"
    assert result["has_eliminatory_blocker"] is False


def test_real_resumes_folder_readable_if_present():
    real_dir = Path("resumes")
    if real_dir.exists() and list(real_dir.glob("*.pdf")):
        lib = ResumeLibrary(str(real_dir))
        assert lib.count >= 1
        for key in lib.keys():
            txt = lib.text_for(key)
            assert len(txt) > 300
        profile = CandidateProfile.load(lib, profile_json_path="/nonexistent/p.json", profile_md_path="/nonexistent/p.md")
        assert profile.email != ""
        assert profile.full_name != ""
