"""HTML message formatters for Telegram notifications (applications, human action, alerts, recommendations)."""

from __future__ import annotations

import html
from datetime import datetime, timezone


def _format_timestamp(iso_ts: str | int | float | None) -> str:
    if iso_ts is None or iso_ts == "" or isinstance(iso_ts, bool):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        if isinstance(iso_ts, (int, float)):
            ts = float(iso_ts)
            if ts > 1e12:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError, OSError, OverflowError):
        return str(iso_ts)


def format_job_message(job: dict, analysis: dict) -> str:
    """Recommendation-only formatter (used when AUTO_APPLY_ENABLED=false)."""
    title = html.escape(job.get("title") or "Vaga")
    company = html.escape(job.get("company") or "—")
    seniority = html.escape(job.get("seniority") or analysis.get("seniority") or "")
    modality = html.escape(job.get("modality") or "")
    match = analysis.get("match", 0)
    rec = analysis.get("recommendation", "")

    rec_emoji = {"apply": "✅", "maybe": "🟡", "skip": "⛔"}.get(rec, "•")

    lines = [
        "<b>🚀 Nova vaga encontrada</b>",
        "",
        f"<b>Empresa:</b> {company}",
        f"<b>Vaga:</b> {title}",
    ]
    if seniority:
        lines.append(f"<b>Senioridade:</b> {seniority}")
    if modality:
        lines.append(f"<b>Modalidade:</b> {modality}")
    lines.append(f"<b>Origem:</b> {html.escape(job.get('source', ''))}")
    lines.append("")
    lines.append(f"<b>Match: {match}%</b> {rec_emoji}")
    lines.append("")

    matched = analysis.get("matched_skills") or []
    if matched:
        lines.append("<b>Tecnologias:</b>")
        lines.append(html.escape(", ".join(matched[:10])))
        lines.append("")

    reasons = analysis.get("reasons") or []
    if reasons:
        lines.append("<b>Por que combina:</b>")
        for r in reasons[:5]:
            lines.append(f"- {html.escape(r)}")
        lines.append("")

    missing = analysis.get("missing_skills") or []
    if missing:
        lines.append("<b>Gaps:</b>")
        for m in missing[:5]:
            lines.append(f"- {html.escape(m)}")
        lines.append("")

    resume_key = analysis.get("resume_key")
    if resume_key:
        lines.append(f"<b>Currículo recomendado:</b> {html.escape(resume_key)}")
        lines.append("")

    changes = analysis.get("resume_changes") or []
    if changes:
        lines.append("<b>Sugestões para melhorar o fit:</b>")
        for c in changes[:5]:
            lines.append(f"- {html.escape(c)}")
        lines.append("")

    url = job.get("url") or ""
    lines.append(f'🔗 <a href="{html.escape(url, quote=True)}">Link da vaga</a>')
    return "\n".join(lines)


def format_application_notification(job: dict, analysis: dict, application: dict) -> str:
    """Format Telegram notification for completed, simulated (dry-run), or pending-human applications."""
    status = application.get("status") or "unknown"
    dry_run = bool(application.get("dry_run"))
    company = html.escape(application.get("company") or job.get("company") or "—")
    title = html.escape(application.get("title") or job.get("title") or "Vaga")
    match_score = application.get("match_score") if application.get("match_score") is not None else analysis.get("match", 0)
    platform = html.escape(application.get("platform") or job.get("source") or "—")
    lang = (application.get("language") or analysis.get("language") or "pt").upper()
    link = application.get("apply_url") or job.get("apply_url") or job.get("url") or ""
    sent_at = _format_timestamp(application.get("applied_at") or application.get("updated_at"))

    if status == "applied" and not dry_run:
        header = "<b>✅ Candidatura enviada com confirmação</b>"
        status_label = "applied (confirmada)"
    elif dry_run and status == "documents_ready":
        header = "<b>🧪 [DRY-RUN] Candidatura simulada e pronta</b>"
        status_label = "documents_ready (simulação — não enviada)"
    elif job.get("source") == "linkedin_posts" and application.get("platform") == "linkedin_post":
        header = "<b>📢 Vaga em postagem no LinkedIn (sem e-mail direto)</b>"
        status_label = "pending_human_action (post LinkedIn)"
    elif status == "pending_human_action":
        header = "<b>⚠️ Intervenção humana necessária</b>"
        status_label = "pending_human_action"
    elif status == "application_failed":
        header = "<b>❌ Falha na candidatura</b>"
        status_label = "application_failed"
    else:
        header = f"<b>ℹ️ Atualização de candidatura ({html.escape(status)})</b>"
        status_label = status

    lines = [
        header,
        "",
        f"<b>Empresa:</b> {company}",
        f"<b>Vaga:</b> {title}",
        f"<b>Compatibilidade:</b> {match_score}% | <b>Idioma:</b> {html.escape(lang)}",
        f"<b>Plataforma:</b> {platform}",
        f"<b>Status:</b> {html.escape(status_label)}",
        f"<b>Data/Horário:</b> {html.escape(sent_at)}",
    ]

    if application.get("confirmation_id"):
        lines.append(f"<b>Comprovante/ID:</b> <code>{html.escape(application['confirmation_id'])}</code>")

    if application.get("base_resume_key"):
        lines.append(f"<b>Currículo-base:</b> {html.escape(application['base_resume_key'])}")

    docs = []
    if application.get("custom_resume_path"):
        docs.append("Currículo personalizado (PDF)")
    if application.get("cover_letter_path"):
        docs.append("Carta de apresentação (PDF)")
    if docs:
        lines.append(f"<b>Documentos anexos:</b> {', '.join(docs)}")

    if application.get("email_to") or application.get("email_subject") or application.get("email_body"):
        lines.append("")
        if application.get("email_to"):
            lines.append(f"<b>E-mail destinatário:</b> <code>{html.escape(str(application['email_to']))}</code>")
        if application.get("email_subject"):
            lines.append(f"<b>Assunto:</b> <code>{html.escape(str(application['email_subject']))}</code>")
        if application.get("email_body"):
            lines.append(
                f"<b>Mensagem enviada/gerada:</b>\n<pre>{html.escape(str(application['email_body'])[:1400])}</pre>"
            )

    if job.get("source") == "linkedin_posts" and job.get("body"):
        excerpt = " ".join(str(job["body"]).split())[:350]
        if len(str(job["body"]).strip()) > 350:
            excerpt += "..."
        lines.append("")
        lines.append(f"<b>Trecho da postagem:</b> {html.escape(excerpt)}")

    reason = application.get("human_action_reason") or application.get("failure_reason")
    if reason:
        lines.append("")
        lines.append(f"<b>Motivo:</b> {html.escape(reason)}")

    if link:
        lines.append("")
        label = "Continuar candidatura manualmente" if status == "pending_human_action" else "Link da oportunidade"
        lines.append(f'🔗 <a href="{html.escape(link, quote=True)}">{label}</a>')

    return "\n".join(lines)


def format_failure_alert(title: str, detail: str, job: dict | None = None) -> str:
    """Format an operational or critical failure alert for Telegram."""
    lines = [
        f"<b>🚨 Alerta bot-job: {html.escape(title)}</b>",
        "",
    ]
    if job:
        lines.append(f"<b>Empresa:</b> {html.escape(job.get('company') or '—')}")
        lines.append(f"<b>Vaga:</b> {html.escape(job.get('title') or '—')}")
        if job.get("url"):
            lines.append(f'🔗 <a href="{html.escape(job["url"], quote=True)}">Abrir vaga</a>')
        lines.append("")
    lines.append(f"<b>Detalhes:</b> {html.escape(detail[:1200])}")
    return "\n".join(lines)
