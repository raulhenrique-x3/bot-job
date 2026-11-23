"""Generic web form applier and humanized SMTP email-application handler."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    PlatformApplier,
)
from bot.appliers.form_filler import PlaywrightFormFiller
from bot.config import env, env_int
from bot.gemini.client import agy_ask, agy_available, extract_json_object
from bot.gemini.prompts import EMAIL_APPLICATION_SYSTEM, EMAIL_APPLICATION_USER
from bot.profile import CandidateProfile

logger = logging.getLogger("jobbot.appliers.generic")

EMAIL_MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["subject", "body"],
}

_SMTP_HOSTS_BY_DOMAIN: dict[str, str] = {
    "gmail.com": "smtp.gmail.com",
    "googlemail.com": "smtp.gmail.com",
    "hotmail.com": "smtp-mail.outlook.com",
    "hotmail.com.br": "smtp-mail.outlook.com",
    "outlook.com": "smtp-mail.outlook.com",
    "outlook.com.br": "smtp-mail.outlook.com",
    "live.com": "smtp-mail.outlook.com",
    "msn.com": "smtp-mail.outlook.com",
    "yahoo.com": "smtp.mail.yahoo.com",
    "yahoo.com.br": "smtp.mail.yahoo.com",
    "icloud.com": "smtp.mail.me.com",
    "me.com": "smtp.mail.me.com",
}


def parse_mailto_url(apply_url: str) -> tuple[str, str]:
    """Parse mailto:address?subject=... into (recipient_email, subject_hint)."""
    clean = (apply_url or "").strip()
    if not clean.lower().startswith("mailto:"):
        return "", ""
    parsed = urlparse(clean)
    recipient = unquote(parsed.path or "").strip()
    qs = parse_qs(parsed.query)
    subject_hint = unquote(qs.get("subject", [""])[0] or "").strip()
    return recipient, subject_hint


def resolve_smtp_config(profile: CandidateProfile) -> dict:
    """Resolve SMTP settings from environment variables and candidate profile email."""
    user = (env("SMTP_USER") or profile.email or "").strip()
    password = env("SMTP_PASSWORD").strip()
    host = env("SMTP_HOST").strip()
    if not host and "@" in user:
        domain = user.split("@", 1)[1].lower().strip()
        host = _SMTP_HOSTS_BY_DOMAIN.get(domain, "")
    port = env_int("SMTP_PORT", 587)
    from_name = (env("SMTP_FROM_NAME") or profile.full_name or "").strip()
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_name": from_name,
    }


def _fallback_humanized_email(
    job: dict,
    profile: CandidateProfile,
    *,
    language: str,
    subject_hint: str = "",
) -> tuple[str, str]:
    role = (job.get("title") or "Desenvolvedor(a)").strip()
    company = (job.get("company") or "").strip()
    name = (profile.full_name or profile.first_name or "Raul Henrique").strip()
    skills = ", ".join(profile.skills[:5]) if profile.skills else "React, TypeScript, Next.js, Node.js, Python e IA/LLMs"

    contact_lines = []
    if profile.linkedin_url:
        contact_lines.append(f"LinkedIn: {profile.linkedin_url}")
    if profile.github_url:
        contact_lines.append(f"GitHub: {profile.github_url}")
    if profile.phone:
        contact_lines.append(f"Telefone/WhatsApp: {profile.phone}")
    contacts = "\n".join(contact_lines)

    if language == "en":
        subject = subject_hint or f"Application: {role} - {name}"
        greeting = f"Hi {company} team," if company and company != "—" else "Hi there,"
        body = (
            f"{greeting}\n\n"
            f"I saw your LinkedIn post about the {role} position and wanted to share my profile directly. "
            f"I have {profile.years_of_experience or '4+'} years of hands-on experience building production web apps and AI workflows "
            f"with {skills}.\n\n"
            "I attached my tailored resume and cover letter in PDF so you can quickly review my recent projects and impact. "
            "If my background aligns with what you need, I'd be glad to have a quick chat this week.\n\n"
            f"Best regards,\n{name}"
        )
        if contacts:
            body += f"\n{contacts}"
        return subject, body

    subject = subject_hint or f"Candidatura: {role} - {name}"
    greeting = f"Olá, pessoal da {company}," if company and company != "—" else "Olá, tudo bem?"
    body = (
        f"{greeting}\n\n"
        f"Vi a postagem de vocês no LinkedIn sobre a vaga de {role} e decidi enviar minha candidatura por aqui. "
        f"Atuo há {profile.years_of_experience or '4+'} anos no desenvolvimento de aplicações web e soluções com IA, "
        f"com foco em {skills}.\n\n"
        "Em anexo estão meu currículo personalizado e uma carta de apresentação rápida detalhando meus projetos mais recentes. "
        "Se fizer sentido para o momento do time, fico totalmente à disposição para conversarmos.\n\n"
        f"Um abraço,\n{name}"
    )
    if contacts:
        body += f"\n{contacts}"
    return subject, body


def compose_application_email(
    job: dict,
    profile: CandidateProfile,
    documents: ApplicationDocuments,
    *,
    subject_hint: str = "",
) -> tuple[str, str]:
    """Generate a concise, humanized application email (subject, body) matching job language."""
    target_lang = "en" if (documents.language or "pt").lower().startswith("en") else "pt"
    lang_label = "Inglês" if target_lang == "en" else "Português"

    resume_excerpt = ""
    if documents.resume_md:
        try:
            resume_excerpt = Path(documents.resume_md).read_text(encoding="utf-8")[:3500]
        except OSError:
            resume_excerpt = ""

    if agy_available():
        prompt = f"{EMAIL_APPLICATION_SYSTEM}\n\n" + EMAIL_APPLICATION_USER.format(
            language=target_lang,
            language_label=lang_label,
            profile_facts=profile.as_verified_facts_text()[:2500],
            tailored_resume_md=resume_excerpt or "(usar fatos verificados acima)",
            company=job.get("company") or "—",
            title=job.get("title") or "—",
            subject_hint=subject_hint or "(não especificado)",
            body=(job.get("body") or "")[:4500],
        )
        try:
            raw = agy_ask(prompt, json_schema=EMAIL_MESSAGE_SCHEMA)
            data = extract_json_object(raw)
            if data and str(data.get("body") or "").strip():
                subject = subject_hint or str(data.get("subject") or "").strip()
                if not subject:
                    subject = (
                        f"Application: {job.get('title') or 'Software Engineer'} - {profile.full_name}"
                        if target_lang == "en"
                        else f"Candidatura: {job.get('title') or 'Desenvolvedor'} - {profile.full_name}"
                    )
                return subject[:160], str(data["body"]).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI email composition failed job=%s err=%s; using fallback", job.get("id"), exc)

    return _fallback_humanized_email(job, profile, language=target_lang, subject_hint=subject_hint)


def send_application_email_smtp(
    smtp_cfg: dict,
    *,
    to_email: str,
    subject: str,
    body: str,
    attachments: list[str],
) -> str:
    """Send an application email with PDF attachments via SMTP/STARTTLS or SMTPS."""
    user = smtp_cfg["user"]
    password = smtp_cfg["password"]
    host = smtp_cfg["host"]
    port = int(smtp_cfg.get("port") or 587)
    from_name = (smtp_cfg.get("from_name") or "").strip()

    msg = EmailMessage()
    msg["From"] = f"{from_name} <{user}>" if from_name else user
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    for raw_path in attachments:
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_file():
            continue
        msg.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=path.name,
        )

    smtp_cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with smtp_cls(host, port, timeout=30) as server:
        server.ehlo()
        if port != 465:
            server.starttls()
            server.ehlo()
        server.login(user, password)
        server.send_message(msg)

    return f"smtp:{to_email}"


class EmailApplier(PlatformApplier):
    """Handles mailto: opportunities by composing a humanized email with Resume + Cover Letter PDFs."""

    platform_name = "email"

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return (apply_url or "").strip().lower().startswith("mailto:")

    def apply(
        self,
        job: dict,
        apply_url: str,
        profile: CandidateProfile,
        documents: ApplicationDocuments,
        *,
        browser_context=None,
        dry_run: bool = True,
        timeout_seconds: int = 60,
        cover_letter_provider: CoverLetterCallback | None = None,
    ) -> ApplicationOutcome:
        recipient, subject_hint = parse_mailto_url(apply_url)
        if not recipient or "@" not in recipient:
            return ApplicationOutcome(
                status="pending_human_action",
                platform=self.platform_name,
                apply_url=apply_url,
                reason=f"Endereço de e-mail inválido para candidatura: {apply_url}",
                dry_run=dry_run,
            )

        cover_pdf = documents.cover_letter_pdf
        if not cover_pdf and cover_letter_provider is not None:
            try:
                cover_pdf, cover_text = cover_letter_provider()
                documents.cover_letter_pdf = cover_pdf
                documents.cover_letter_text = cover_text
            except Exception as exc:  # noqa: BLE001
                logger.warning("cover letter generation failed before email job=%s: %s", job.get("id"), exc)

        subject, email_body = compose_application_email(
            job,
            profile,
            documents,
            subject_hint=subject_hint,
        )
        filled = {
            "to": recipient,
            "subject": subject,
            "email_body": email_body,
        }

        if documents.resume_pdf:
            try:
                out_dir = Path(documents.resume_pdf).parent
                if out_dir.exists():
                    (out_dir / "email.txt").write_text(
                        f"To: {recipient}\nSubject: {subject}\n\n{email_body}\n",
                        encoding="utf-8",
                    )
            except OSError:
                pass

        if dry_run:
            return ApplicationOutcome(
                status="documents_ready",
                platform=self.platform_name,
                apply_url=apply_url,
                cover_letter_requested=True,
                filled_fields=filled,
                dry_run=True,
            )

        smtp_cfg = resolve_smtp_config(profile)
        if not (smtp_cfg["user"] and smtp_cfg["password"] and smtp_cfg["host"]):
            return ApplicationOutcome(
                status="pending_human_action",
                platform=self.platform_name,
                apply_url=apply_url,
                cover_letter_requested=True,
                filled_fields=filled,
                reason=(
                    f"E-mail humanizado pronto para {recipient} com currículo e carta anexos "
                    "(configure SMTP_USER e SMTP_PASSWORD no .env para envio automático)."
                ),
                dry_run=False,
            )

        try:
            conf_id = send_application_email_smtp(
                smtp_cfg,
                to_email=recipient,
                subject=subject,
                body=email_body,
                attachments=[p for p in (documents.resume_pdf, cover_pdf) if p],
            )
            return ApplicationOutcome(
                status="applied",
                platform=self.platform_name,
                apply_url=apply_url,
                confirmation_id=conf_id,
                cover_letter_requested=True,
                filled_fields=filled,
                dry_run=False,
            )
        except (smtplib.SMTPException, OSError) as exc:
            logger.warning("SMTP send failed to=%s job=%s: %s", recipient, job.get("id"), exc)
            return ApplicationOutcome(
                status="pending_human_action",
                platform=self.platform_name,
                apply_url=apply_url,
                cover_letter_requested=True,
                filled_fields=filled,
                reason=(
                    f"Falha ao enviar e-mail via SMTP para {recipient} ({exc}). "
                    "Use o texto humanizado e os PDFs anexos no Telegram."
                ),
                dry_run=False,
            )


class GenericWebApplier(PlatformApplier):
    """Fallback Playwright applier for standard HTML/ATS forms without a dedicated integration."""

    platform_name = "generic"

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return (apply_url or "").startswith(("http://", "https://"))

    def apply(
        self,
        job: dict,
        apply_url: str,
        profile: CandidateProfile,
        documents: ApplicationDocuments,
        *,
        browser_context=None,
        dry_run: bool = True,
        timeout_seconds: int = 60,
        cover_letter_provider: CoverLetterCallback | None = None,
    ) -> ApplicationOutcome:
        if browser_context is None:
            return ApplicationOutcome(
                status="application_failed",
                platform=self.platform_name,
                apply_url=apply_url,
                reason="Contexto de navegador indisponível para formulário web.",
                retryable=True,
                dry_run=dry_run,
            )

        from bot.scrapers.browser import goto

        filler = PlaywrightFormFiller(profile)
        with goto(browser_context, apply_url, timeout_ms=timeout_seconds * 1000) as page:
            if page is None:
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=apply_url,
                    reason=f"Não foi possível acessar URL de candidatura: {apply_url}",
                    retryable=True,
                    dry_run=dry_run,
                )
            return filler.run_on_page(
                page,
                platform=self.platform_name,
                apply_url=page.url or apply_url,
                job=job,
                documents=documents,
                dry_run=dry_run,
                timeout_seconds=timeout_seconds,
                cover_letter_provider=cover_letter_provider,
            )
