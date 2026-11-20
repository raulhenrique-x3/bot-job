"""End-to-end integration tests for the full automated job application pipeline."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from bot.appliers import ApplicationOutcome, ApplierRegistry, PlatformApplier
from bot.database.db import Database
from bot.documents.storage import sanitize_job_id
from bot.main import run_once
from bot.scrapers.base import JobSource
from tests.conftest import SAMPLE_RESUME_EN, SAMPLE_RESUME_PT


class _FakeScraper(JobSource):
    name = "fake_portal"

    def __init__(self, jobs: list[dict]):
        self._jobs = jobs

    def scrape(self) -> list[dict]:
        return list(self._jobs)


class _FakeTelegram:
    def __init__(self):
        self.bundles: list[dict] = []
        self.alerts: list[str] = []
        self.legacy_jobs: list[tuple[str, str | None]] = []

    def send_application_bundle(self, message, *, resume_path=None, cover_letter_path=None, job_slug=""):
        self.bundles.append({
            "message": message,
            "resume_path": resume_path,
            "cover_letter_path": cover_letter_path,
            "job_slug": job_slug,
        })
        return True

    def send_alert(self, message):
        self.alerts.append(message)
        return True

    def send_job(self, message, resume_path):
        self.legacy_jobs.append((message, resume_path))
        return True


class _FakeATSApplier(PlatformApplier):
    platform_name = "greenhouse"

    def __init__(self, mode: str = "apply_with_cover"):
        self.mode = mode
        self.calls = 0

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return True

    def apply(self, job, apply_url, profile, documents, *, dry_run=True, cover_letter_provider=None, **kwargs):
        self.calls += 1
        assert Path(documents.resume_pdf).exists()
        if self.mode == "captcha":
            return ApplicationOutcome(
                status="pending_human_action",
                platform="greenhouse",
                apply_url=apply_url,
                reason="CAPTCHA detectado na plataforma.",
                dry_run=dry_run,
            )
        if self.mode == "apply_with_cover" and cover_letter_provider is not None:
            cl_pdf, cl_text = cover_letter_provider()
            assert Path(cl_pdf).exists()
            assert len(cl_text) > 50
            if dry_run:
                return ApplicationOutcome(
                    status="documents_ready",
                    platform="greenhouse",
                    apply_url=apply_url,
                    reason="[DRY-RUN] pronto",
                    cover_letter_requested=True,
                    dry_run=True,
                )
            return ApplicationOutcome(
                status="applied",
                platform="greenhouse",
                apply_url=apply_url,
                confirmation_id="CONF-E2E-01",
                cover_letter_requested=True,
                dry_run=False,
            )
        return ApplicationOutcome(
            status="documents_ready" if dry_run else "applied",
            platform="greenhouse",
            apply_url=apply_url,
            confirmation_id="" if dry_run else "CONF-E2E-02",
            cover_letter_requested=False,
            dry_run=dry_run,
        )


def _patch_ai(monkeypatch, analysis_by_id: dict[str, dict]):
    monkeypatch.setattr("bot.gemini.analysis.agy_available", lambda: True)

    def fake_agy_ask(prompt: str, json_schema=None, **kwargs) -> str:
        # Distinguish analysis vs resume vs cover letter calls by prompt header
        if "# TAREFA" in prompt:
            for jid, payload in analysis_by_id.items():
                if jid in prompt:
                    return json.dumps(payload)
            return json.dumps(next(iter(analysis_by_id.values())))
        if "IDIOMA ALVO DO CURRÍCULO: Inglês" in prompt:
            return json.dumps(SAMPLE_RESUME_EN)
        if "IDIOMA ALVO DO CURRÍCULO" in prompt:
            return json.dumps(SAMPLE_RESUME_PT)
        if "carta de apresentação" in prompt.lower():
            return json.dumps({
                "recipient_line": "Hiring Team — GlobalTech",
                "salutation": "Dear Hiring Team,",
                "paragraphs": [
                    "I am excited to apply for the Fullstack Engineer role at GlobalTech based on my 4+ years building React, TypeScript, Node.js, and FastAPI systems at BSATech.",
                    "My recent work reduced load times by ~30% and kept REST microservice latencies under 200 ms in production.",
                    "I look forward to discussing how my hands-on experience aligns with your team.",
                ],
                "closing": "Best regards,",
                "signature": "Raul Henrique",
            })
        return "{}"

    monkeypatch.setattr("bot.gemini.analysis.agy_ask", fake_agy_ask)
    monkeypatch.setattr("bot.documents.resume_generator.agy_ask", fake_agy_ask)
    monkeypatch.setattr("bot.documents.cover_letter.agy_ask", fake_agy_ask)


def test_end_to_end_live_pipeline_with_rejection_and_idempotency(monkeypatch, test_settings, sample_profile):
    jobs = [
        {
            "id": "gh:1001",
            "source": "fake_portal",
            "url": "https://boards.greenhouse.io/globaltech/jobs/1001",
            "title": "Fullstack Engineer",
            "company": "GlobalTech",
            "location": "Remote",
            "modality": "Remote",
            "body": "Looking for Python FastAPI React TypeScript engineer.",
        },
        {
            "id": "gh:1002",
            "source": "fake_portal",
            "url": "https://boards.greenhouse.io/blocked/jobs/1002",
            "title": "Senior Rust Kernel Engineer",
            "company": "DefenseCorp",
            "location": "Remote",
            "body": "Must have 10 years of Linux kernel Rust experience.",
        },
    ]
    _patch_ai(
        monkeypatch,
        {
            "GlobalTech": {
                "match": 90,
                "recommendation": "apply",
                "language": "en",
                "has_eliminatory_blocker": False,
                "eliminatory_blockers": [],
                "matched_skills": ["Python", "React", "TypeScript"],
                "missing_required_skills": [],
                "missing_skills": [],
                "reasons": ["Great fullstack match"],
                "resume_key": "resume_raul_fullstack",
                "resume_changes": ["Emphasize FastAPI"],
            },
            "DefenseCorp": {
                "match": 20,
                "recommendation": "skip",
                "language": "en",
                "has_eliminatory_blocker": True,
                "eliminatory_blockers": ["Requires US citizenship"],
                "matched_skills": [],
                "missing_required_skills": ["Rust", "US Citizenship"],
                "missing_skills": ["Rust"],
                "reasons": ["Eliminatory citizenship blocker"],
                "resume_key": "resume_raul_fullstack",
                "resume_changes": [],
            },
        },
    )

    applier = _FakeATSApplier(mode="apply_with_cover")
    registry = ApplierRegistry([applier])
    tg = _FakeTelegram()

    summary1 = run_once(
        settings=test_settings,
        scrapers_override=[_FakeScraper(jobs)],
        registry_override=registry,
        telegram_override=tg,
    )
    assert summary1["analyzed"] == 2
    assert summary1["rejected"] == 1
    assert summary1["applied"] == 1
    assert applier.calls == 1

    # Verify artifacts saved under data/applications/gh_1001/ with human-standard PDF filenames
    app_dir = Path(test_settings.applications_dir) / sanitize_job_id("gh:1001")
    assert (app_dir / "Raul_Henrique_Resume.pdf").exists()
    assert (app_dir / "resume.md").exists()
    assert (app_dir / "Raul_Henrique_Cover_Letter.pdf").exists()
    assert (app_dir / "cover_letter.md").exists()
    meta = json.loads((app_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["confirmation_id"] == "CONF-E2E-01"
    assert meta["status"] == "applied"

    # Rejected job must NOT have generated documents
    assert not (Path(test_settings.applications_dir) / sanitize_job_id("gh:1002")).exists()

    # Telegram bundle received with both PDFs using human-standard filenames
    assert len(tg.bundles) == 1
    assert tg.bundles[0]["resume_path"].endswith("Raul_Henrique_Resume.pdf")
    assert tg.bundles[0]["cover_letter_path"].endswith("Raul_Henrique_Cover_Letter.pdf")
    assert "CONF-E2E-01" in tg.bundles[0]["message"]

    # Second run is 100% idempotent — does NOT re-apply!
    summary2 = run_once(
        settings=test_settings,
        scrapers_override=[_FakeScraper(jobs)],
        registry_override=registry,
        telegram_override=tg,
    )
    assert summary2["applied"] == 0
    assert applier.calls == 1


def test_end_to_end_dry_run_and_captcha_escalation(monkeypatch, test_settings, sample_profile):
    dry_settings = replace(test_settings, application_dry_run=True)
    jobs = [
        {
            "id": "gh:2001",
            "source": "fake_portal",
            "url": "https://boards.greenhouse.io/acme/jobs/2001",
            "title": "Desenvolvedor Fullstack",
            "company": "Acme Brasil",
            "body": "Vaga remota Python React Node.js.",
        }
    ]
    _patch_ai(
        monkeypatch,
        {
            "Acme Brasil": {
                "match": 86,
                "recommendation": "apply",
                "language": "pt",
                "has_eliminatory_blocker": False,
                "eliminatory_blockers": [],
                "matched_skills": ["Python", "React"],
                "missing_required_skills": [],
                "missing_skills": [],
                "reasons": ["Alta aderência"],
                "resume_key": "curriculo_raul_fullstack",
                "resume_changes": [],
            }
        },
    )

    # Dry-run: generates resume + cover letter, does NOT mark applied
    tg_dry = _FakeTelegram()
    res_dry = run_once(
        settings=dry_settings,
        scrapers_override=[_FakeScraper(jobs)],
        registry_override=ApplierRegistry([_FakeATSApplier(mode="apply_with_cover")]),
        telegram_override=tg_dry,
    )
    assert res_dry["dry_run_ready"] == 1
    assert res_dry["applied"] == 0
    assert "[DRY-RUN]" in tg_dry.bundles[0]["message"]

    # Repeated dry-run is idempotent (no duplicate notifications)
    res_dry_again = run_once(
        settings=dry_settings,
        scrapers_override=[_FakeScraper(jobs)],
        registry_override=ApplierRegistry([_FakeATSApplier(mode="apply_with_cover")]),
        telegram_override=tg_dry,
    )
    assert res_dry_again["dry_run_ready"] == 0
    assert len(tg_dry.bundles) == 1

    # CAPTCHA escalation on a fresh DB -> pending_human_action + link + resume attached
    cap_settings = replace(test_settings, database_path=str(Path(test_settings.database_path).parent / "cap.db"))
    jobs_cap = [
        {
            "id": "gh:2002",
            "source": "fake_portal",
            "url": "https://boards.greenhouse.io/cap/jobs/2002",
            "title": "Desenvolvedor Python",
            "company": "CapTech",
            "body": "Vaga remota Python FastAPI.",
        }
    ]
    tg_cap = _FakeTelegram()
    res_cap = run_once(
        settings=cap_settings,
        scrapers_override=[_FakeScraper(jobs_cap)],
        registry_override=ApplierRegistry([_FakeATSApplier(mode="captcha")]),
        telegram_override=tg_cap,
    )
    assert res_cap["pending_human"] == 1
    assert "CAPTCHA" in tg_cap.bundles[0]["message"]
    assert tg_cap.bundles[0]["resume_path"].endswith("Raul_Henrique_Curriculo.pdf")

    db = Database(cap_settings.database_path)
    try:
        assert db.get_application("gh:2002")["status"] == "pending_human_action"
    finally:
        db.close()
