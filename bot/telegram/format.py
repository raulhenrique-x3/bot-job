import html


def format_job_message(job: dict, analysis: dict) -> str:
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
    if analysis.get("seniority"):
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
    lines.append(f"🔗 <a href=\"{html.escape(url, quote=True)}\">Link da vaga</a>")
    return "\n".join(lines)
