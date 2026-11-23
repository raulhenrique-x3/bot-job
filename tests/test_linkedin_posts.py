"""Tests for LinkedIn feed posts scraper, humanized SMTP email applier, and fast-track notifications."""

from __future__ import annotations

import json
import smtplib
from dataclasses import replace
from pathlib import Path

from bot.appliers import ApplicationDocuments, ApplierRegistry
from bot.appliers.generic import EmailApplier
from bot.database.db import Database
from bot.main import run_once
from bot.scrapers.base import JobSource
from bot.scrapers.linkedin_posts import (
    LinkedInPostsScraper,
    extract_email_subject_hint,
    extract_recruitment_emails,
    is_hiring_post,
    is_target_role_post,
)
from tests.conftest import SAMPLE_RESUME_EN, SAMPLE_RESUME_PT


def _sample_posts_html() -> str:
    return """
    <html><body>
      <!-- 1. Hiring post with email and subject hint -->
      <div componentkey="expandedPostEmail001FeedType_FLAGSHIP_SEARCH">
        <a href="https://www.linkedin.com/in/recrutadora-tech/">Marina Silva • 3º e +</a>
        <span data-testid="expandable-text-box">
          Vaga Aberta: Desenvolvedor(a) Fullstack React + Node + IA (100% Remoto)
          Empresa: NeuralStack Brasil
          Estamos contratando dev fullstack com experiência em React, TypeScript e Python/LLMs.
          Interessados enviar currículo para talentos@neuralstack.com.br com o assunto: "Vaga Fullstack IA".
        </span>
      </div>

      <!-- 2. Hiring post WITHOUT email (DM/inbox only) -->
      <div componentkey="expandedPostInbox002FeedType_FLAGSHIP_SEARCH">
        <a href="https://www.linkedin.com/company/aurora-ai/">Aurora AI Labs</a>
        <span data-testid="expandable-text-box">
          We are hiring a Forward Deployed Engineer (Remote / LATAM) to work on LLM agent deployments.
          Strong React, Python, and AI engineer background required.
          Comenta "eu quero" ou me chama no inbox que explico os detalhes!
        </span>
      </div>

      <!-- 3. Job-seeker post WITH email (must be rejected!) -->
      <div componentkey="expandedPostSeeker003FeedType_FLAGSHIP_SEARCH">
        <a href="https://www.linkedin.com/in/candidato-dev/">Guilherme Souza</a>
        <span data-testid="expandable-text-box">
          🚀 Em busca da minha primeira oportunidade na área de tecnologia! #opentowork
          Sou desenvolvedor Fullstack React e Python, meu currículo pode ser pedido em candidato@gmail.com.
        </span>
      </div>

      <!-- 4. Unrelated career tips post (must be rejected!) -->
      <div componentkey="expandedPostBlog004FeedType_FLAGSHIP_SEARCH">
        <a href="https://www.linkedin.com/in/tech-writer/">Dicas Tech</a>
        <span data-testid="expandable-text-box">
          Não envie mais um currículo no automático sem antes revisar seu LinkedIn.
          Veja 5 dicas de React e Python para melhorar seu ATS score hoje mesmo.
        </span>
      </div>
    </body></html>
    """


def test_linkedin_posts_classifier_and_sdui_parser():
    assert is_target_role_post("Hiring Forward Deployed Engineer") is True
    assert is_target_role_post("Vaga Dev IA / AI Engineer") is True
    assert is_target_role_post("Frontend React TypeScript Developer") is True
    assert is_target_role_post("Analista Contábil Sênior") is False

    assert extract_recruitment_emails(
        "Enviar CV para rh@empresa.com.br; nunca enviar para example@example.com ou logo@2x.png.",
        links=["mailto:vagas@startup.ai?subject=Frontend"],
    ) == ["vagas@startup.ai", "rh@empresa.com.br"]
    assert extract_email_subject_hint('Assunto: "Vaga Fullstack IA"') == "Vaga Fullstack IA"

    assert is_hiring_post("Não envie mais um currículo sem revisar antes para vagas Fullstack.") is False
    assert is_hiring_post("Busco oportunidade como desenvolvedor Front-end React remoto.") is False

    scraper = LinkedInPostsScraper(browser=None)
    posts = scraper._parse_posts(_sample_posts_html(), search_url="https://www.linkedin.com/search/results/content/")
    assert len(posts) == 2

    email_post = posts[0]
    assert email_post["id"] == "lip:PostEmail001"
    assert email_post["source"] == "linkedin_posts"
    assert email_post["company"] == "NeuralStack Brasil"
    assert email_post["apply_url"] == "mailto:talentos@neuralstack.com.br?subject=Vaga+Fullstack+IA"
    assert email_post["url"] == "https://www.linkedin.com/in/recrutadora-tech/recent-activity/all/"
    assert email_post["remote"] is True

    inbox_post = posts[1]
    assert inbox_post["id"] == "lip:PostInbox002"
    assert inbox_post["apply_url"] == ""
    assert inbox_post["url"] == "https://www.linkedin.com/company/aurora-ai/recent-activity/all/"
    assert "Forward Deployed Engineer" in inbox_post["title"]


def test_email_applier_dry_run_no_smtp_and_live_smtp(monkeypatch, tmp_path, sample_profile):
    monkeypatch.setattr("bot.appliers.generic.agy_available", lambda: True)
    monkeypatch.setattr(
        "bot.appliers.generic.agy_ask",
        lambda prompt, **kw: json.dumps({
            "subject": "Assunto AI",
            "body": "Olá pessoal da NeuralStack,\n\nVi a vaga no LinkedIn e atuo há 4+ anos com React, TypeScript e Python/LLMs.\n\nSeguem meu currículo e carta de apresentação em anexo.",
        }),
    )

    resume_pdf = tmp_path / "Raul_Henrique_Curriculo.pdf"
    resume_md = tmp_path / "resume.md"
    cover_pdf = tmp_path / "Raul_Henrique_Carta_Apresentacao.pdf"
    resume_pdf.write_bytes(b"%PDF-1.4 resume")
    resume_md.write_text("# Raul Henrique\nReact, Python, LLMs", encoding="utf-8")
    cover_pdf.write_bytes(b"%PDF-1.4 cover")

    cl_calls = 0

    def fake_cl_provider():
        nonlocal cl_calls
        cl_calls += 1
        return str(cover_pdf), "Carta personalizada"

    docs = ApplicationDocuments(resume_pdf=str(resume_pdf), resume_md=str(resume_md), language="pt")
    job = {"id": "lip:PostEmail001", "title": "Desenvolvedor Fullstack IA", "company": "NeuralStack Brasil", "body": "Vaga Fullstack."}
    mailto_url = "mailto:talentos@neuralstack.com.br?subject=Vaga+Fullstack+IA"

    applier = EmailApplier()
    assert applier.can_handle(mailto_url, job) is True

    # 1. Dry-run generates cover letter, composes humanized email, saves email.txt, does not send SMTP
    dry_outcome = applier.apply(
        job, mailto_url, sample_profile, docs, dry_run=True, cover_letter_provider=fake_cl_provider
    )
    assert dry_outcome.status == "documents_ready"
    assert dry_outcome.cover_letter_requested is True
    assert cl_calls == 1
    assert dry_outcome.filled_fields["to"] == "talentos@neuralstack.com.br"
    assert dry_outcome.filled_fields["subject"] == "Vaga Fullstack IA"
    assert "NeuralStack" in dry_outcome.filled_fields["email_body"]
    assert (tmp_path / "email.txt").exists()

    # 2. Live without SMTP_PASSWORD returns pending_human_action with humanized draft ready for Telegram
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    no_smtp_outcome = applier.apply(job, mailto_url, sample_profile, docs, dry_run=False)
    assert no_smtp_outcome.status == "pending_human_action"
    assert "SMTP_PASSWORD" in no_smtp_outcome.reason
    assert no_smtp_outcome.filled_fields["email_body"] == dry_outcome.filled_fields["email_body"]

    # 3. Live with SMTP credentials sends email with BOTH PDFs attached
    sent_messages = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=30):
            self.host = host
            self.port = port
            self.tls_started = False
            self.logged_in = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            self.tls_started = True

        def login(self, user, password):
            self.logged_in = (user, password)

        def send_message(self, msg):
            sent_messages.append((self.host, self.port, self.tls_started, self.logged_in, msg))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "raul@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-secret-pass")

    live_outcome = applier.apply(job, mailto_url, sample_profile, docs, dry_run=False)
    assert live_outcome.status == "applied"
    assert live_outcome.confirmation_id == "smtp:talentos@neuralstack.com.br"
    assert len(sent_messages) == 1
    host, port, tls_started, logged_in, msg = sent_messages[0]
    assert host == "smtp.example.com"
    assert port == 587
    assert tls_started is True
    assert logged_in == ("raul@example.com", "app-secret-pass")
    assert msg["To"] == "talentos@neuralstack.com.br"
    assert msg["Subject"] == "Vaga Fullstack IA"
    attached_names = [part.get_filename() for part in msg.iter_attachments()]
    assert attached_names == ["Raul_Henrique_Curriculo.pdf", "Raul_Henrique_Carta_Apresentacao.pdf"]


def test_end_to_end_linkedin_posts_email_and_no_email_telegram(monkeypatch, test_settings, sample_profile):
    class _FakePostsScraper(JobSource):
        name = "linkedin_posts"

        def scrape(self) -> list[dict]:
            return LinkedInPostsScraper(browser=None)._parse_posts(_sample_posts_html())

    class _RecordingTelegram:
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

    # Force AI analysis to score low/skip so fast-track target-role override is exercised too
    monkeypatch.setattr("bot.gemini.analysis.agy_available", lambda: True)
    monkeypatch.setattr("bot.appliers.generic.agy_available", lambda: True)

    def fake_agy_ask(prompt: str, json_schema=None, **kwargs) -> str:
        if "# TAREFA" in prompt:
            return json.dumps({
                "match": 58,
                "recommendation": "skip",
                "language": "en" if "Forward Deployed" in prompt else "pt",
                "has_eliminatory_blocker": True,
                "eliminatory_blockers": ["Postagem curta sem detalhes completos"],
                "matched_skills": ["React", "Python"],
                "missing_required_skills": [],
                "missing_skills": [],
                "reasons": ["Vaga compatível com foco target"],
                "resume_key": "resume_raul_fullstack" if "Forward Deployed" in prompt else "curriculo_raul_fullstack",
                "resume_changes": [],
            })
        if "IDIOMA ALVO DO CURRÍCULO: Inglês" in prompt:
            return json.dumps(SAMPLE_RESUME_EN)
        if "IDIOMA ALVO DO CURRÍCULO" in prompt:
            return json.dumps(SAMPLE_RESUME_PT)
        if "Gere um assunto objetivo" in prompt:
            return json.dumps({
                "subject": "Vaga Fullstack IA",
                "body": "Olá Marina,\n\nVi sua postagem sobre a vaga Fullstack React + Node + IA na NeuralStack e decidi enviar meu perfil. Atuo há 4+ anos com React, TypeScript, Python e LLMs.\n\nSeguem meu currículo e carta de apresentação em anexo.",
            })
        if "carta de apresentação" in prompt.lower():
            return json.dumps({
                "recipient_line": "Hiring Team",
                "salutation": "Olá,",
                "paragraphs": [
                    "Tenho mais de 4 anos de experiência construindo aplicações com React, TypeScript, Node.js e Python/LLMs.",
                    "Na BSATech atuei em sistemas web reduzindo carregamento em ~30% e mantendo APIs REST abaixo de 200 ms.",
                    "Fico à disposição para conversarmos sobre como posso contribuir no time.",
                ],
                "closing": "Abraço,",
                "signature": "Raul Henrique",
            })
        return "{}"

    monkeypatch.setattr("bot.gemini.analysis.agy_ask", fake_agy_ask)
    monkeypatch.setattr("bot.documents.resume_generator.agy_ask", fake_agy_ask)
    monkeypatch.setattr("bot.documents.cover_letter.agy_ask", fake_agy_ask)
    monkeypatch.setattr("bot.appliers.generic.agy_ask", fake_agy_ask)

    sent_smtp = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=30):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg):
            sent_smtp.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "raul@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-secret")

    tg = _RecordingTelegram()
    cfg = replace(test_settings, application_dry_run=False, max_applications_per_run=5)
    summary = run_once(
        settings=cfg,
        scrapers_override=[_FakePostsScraper()],
        registry_override=ApplierRegistry(),
        telegram_override=tg,
    )

    assert summary["collected"] == 2
    assert summary["analyzed"] == 2
    assert summary["rejected"] == 0
    assert summary["applied"] == 1
    assert summary["pending_human"] == 1
    assert summary["notified"] == 2

    # 1. Email post sent automatically with BOTH Resume + Cover Letter PDFs and notified to Telegram
    assert len(sent_smtp) == 1
    assert len(send_attachments := [p.get_filename() for p in sent_smtp[0].iter_attachments()]) == 2
    assert any("Curriculo" in n for n in send_attachments)
    assert any("Carta" in n for n in send_attachments)

    assert len(tg.bundles) == 2
    email_bundle = next(b for b in tg.bundles if "talentos@neuralstack.com.br" in b["message"])
    assert Path(email_bundle["resume_path"]).exists()
    assert Path(email_bundle["cover_letter_path"]).exists()
    assert "Vaga Fullstack IA" in email_bundle["message"]
    assert "Olá Marina" in email_bundle["message"]

    # 2. No-email LinkedIn post triggered Telegram alert with tailored Resume PDF & post excerpt
    post_bundle = next(b for b in tg.bundles if "Aurora AI Labs" in b["message"])
    assert "postagem no LinkedIn (sem e-mail direto)" in post_bundle["message"]
    assert "Forward Deployed Engineer" in post_bundle["message"]
    assert Path(post_bundle["resume_path"]).exists()
    assert post_bundle["cover_letter_path"] is None

    db = Database(cfg.database_path)
    assert db.get_application("lip:PostEmail001")["status"] == "applied"
    assert db.get_application("lip:PostInbox002")["status"] == "pending_human_action"
    assert db.get_application("lip:PostInbox002")["platform"] == "linkedin_post"
    db.close()
