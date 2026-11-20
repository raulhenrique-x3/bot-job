"""Tests for Telegram notification formatting and multi-PDF bundle sending."""

from __future__ import annotations

from pathlib import Path

from bot.telegram.client import TelegramClient
from bot.telegram.format import (
    format_application_notification,
    format_failure_alert,
    format_job_message,
)


def test_format_application_notification_all_states():
    job = {"title": "Fullstack Engineer", "company": "Acme <Inc>", "url": "https://boards.greenhouse.io/acme/jobs/1", "source": "greenhouse"}
    analysis = {"match": 89, "language": "en"}

    applied_msg = format_application_notification(
        job,
        analysis,
        {
            "status": "applied",
            "platform": "greenhouse",
            "match_score": 89,
            "language": "en",
            "apply_url": job["url"],
            "applied_at": "2026-11-20T10:30:00+00:00",
            "confirmation_id": "GH-555",
            "base_resume_key": "resume_raul_fullstack",
            "custom_resume_path": "data/applications/gh_1/resume.pdf",
            "cover_letter_path": "data/applications/gh_1/cover_letter.pdf",
            "dry_run": 0,
        },
    )
    assert "Candidatura enviada com confirmação" in applied_msg
    assert "Acme &lt;Inc&gt;" in applied_msg
    assert "89%" in applied_msg
    assert "GH-555" in applied_msg
    assert "Carta de apresentação (PDF)" in applied_msg

    human_msg = format_application_notification(
        job,
        analysis,
        {
            "status": "pending_human_action",
            "platform": "gupy",
            "match_score": 89,
            "apply_url": "https://acme.gupy.io/jobs/1",
            "human_action_reason": "CAPTCHA detectado na página.",
            "custom_resume_path": "data/applications/gh_1/resume.pdf",
        },
    )
    assert "Intervenção humana necessária" in human_msg
    assert "CAPTCHA detectado" in human_msg
    assert "Continuar candidatura manualmente" in human_msg

    dry_msg = format_application_notification(
        job,
        analysis,
        {
            "status": "documents_ready",
            "platform": "greenhouse",
            "dry_run": 1,
            "custom_resume_path": "data/applications/gh_1/resume.pdf",
        },
    )
    assert "[DRY-RUN]" in dry_msg


def test_format_failure_alert_and_legacy_job_message():
    alert = format_failure_alert("Erro crítico", "Falha após 3 tentativas", job={"company": "Acme", "title": "Dev", "url": "https://x"})
    assert "Erro crítico" in alert
    assert "Acme" in alert

    legacy = format_job_message(
        {"title": "Dev", "company": "Acme", "source": "nerdin", "url": "https://x"},
        {"match": 80, "recommendation": "apply", "matched_skills": ["Python"], "reasons": ["Good fit"], "resume_key": "curriculo_raul_fullstack"},
    )
    assert "Match: 80%" in legacy


def test_telegram_client_send_application_bundle(tmp_path: Path):
    r_pdf = tmp_path / "Raul_Henrique_Resume.pdf"
    c_pdf = tmp_path / "Raul_Henrique_Cover_Letter.pdf"
    r_pdf.write_bytes(b"%PDF-1.4 resume")
    c_pdf.write_bytes(b"%PDF-1.4 cover")

    calls: list[tuple[str, dict]] = []
    client = TelegramClient(token="123:fake", chat_id="999")
    client._call = lambda method, **kw: calls.append((method, kw)) or {"ok": True}  # type: ignore[method-assign]

    assert client.send_application_bundle(
        "<b>Sent</b>",
        resume_path=str(r_pdf),
        cover_letter_path=str(c_pdf),
        job_slug="gh_1",
    )
    assert [c[0] for c in calls] == ["sendMessage", "sendDocument", "sendDocument"]
    assert calls[1][1]["multipart"][0]["filename"] == "Raul_Henrique_Resume.pdf"
    assert calls[2][1]["multipart"][0]["filename"] == "Raul_Henrique_Cover_Letter.pdf"
