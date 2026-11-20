"""Shared verified-fact answerer and multi-step Playwright form inspection/filling engine."""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    FormField,
    HumanInterventionRequired,
    detect_active_captcha_on_page,
    detect_mfa_or_auth_wall,
    detect_recommendation_letter_request,
    detect_sensitive_or_legal_field,
    detect_submission_confirmation,
    is_cover_letter_label,
    is_privacy_terms_checkbox,
)
from bot.appliers.salary import SalaryBudgetEstimator
from bot.gemini.client import agy_ask, agy_available, extract_json_object
from bot.gemini.prompts import FORM_QA_SYSTEM, FORM_QA_USER
from bot.profile import CandidateProfile

logger = logging.getLogger("jobbot.appliers.form_filler")

FORM_QA_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confident": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["answer", "confident", "reason"],
}

_SUBMIT_BUTTON_RE = re.compile(
    r"^(submit|submit application|apply|apply now|send application|send|"
    r"enviar|enviar candidatura|finalizar candidatura|concluir candidatura|"
    r"candidatar-se|candidatar para a vaga|finalizar|confirmar candidatura)$",
    re.IGNORECASE,
)

_NEXT_STEP_BUTTON_RE = re.compile(
    r"^(next|continue|review|avancar|avançar|proximo|próximo|continuar|revisar|próxima etapa|proxima etapa)$",
    re.IGNORECASE,
)

_ERROR_TEXT_RE = re.compile(
    r"(campo obrigat[óo]rio|preencha este campo|required field|is required|"
    r"invalid email|e-mail inv[áa]lido|telefone inv[áa]lido|formato inv[áa]lido|"
    r"please enter|please select|voc[êe] precisa aceitar)",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).strip()


def _digits_only(text: str) -> str:
    return re.sub(r"\D+", "", text or "")


class VerifiedFactAnswerer:
    """Answers form fields deterministically from profile + Glassdoor salary research + strict AI Q&A."""

    def __init__(
        self,
        profile: CandidateProfile,
        *,
        allow_ai: bool = True,
        allow_salary_web: bool = True,
    ):
        self._profile = profile
        self._allow_ai = allow_ai and agy_available()
        self._salary_estimator = SalaryBudgetEstimator(
            profile, allow_web=allow_salary_web, allow_ai=allow_ai
        )

    def answer_field(
        self,
        field: FormField,
        *,
        job: dict,
        resume_excerpt: str = "",
        cover_letter_text: str = "",
    ) -> str | None:
        """Return verified answer for a field, or raise HumanInterventionRequired if unsafe/ambiguous."""
        label_and_name = f"{field.label} {field.name}".strip()
        lower = label_and_name.lower()

        # Standard Privacy Policy / Terms of Use checkbox (e.g. GeekHunter / ATS consent)
        if field.field_type == "checkbox" and is_privacy_terms_checkbox(label_and_name):
            return "true"

        # Hard safety checks first
        rec_err = detect_recommendation_letter_request(label_and_name)
        if rec_err:
            raise HumanInterventionRequired(rec_err, category="recommendation_letter")

        # Note: asking "Possui empresa aberta / CNPJ?" (select/radio/boolean) is NOT asking for the CNPJ number itself.
        is_cnpj_boolean = (
            any(k in lower for k in ("empresa aberta", "possui_empresa", "possui cnpj", "tem cnpj", "have cnpj"))
            and field.field_type in ("select", "radio", "checkbox")
        )
        if not is_cnpj_boolean:
            sensitive_err = detect_sensitive_or_legal_field(label_and_name)
            if sensitive_err:
                if field.required:
                    raise HumanInterventionRequired(sensitive_err, category="legal_or_sensitive")
                return None

        # Cover letter text field
        if is_cover_letter_label(label_and_name) and field.field_type in ("textarea", "text"):
            return cover_letter_text or None

        # Dynamic salary expectation with Glassdoor / job-scope budget research
        # Never fabricate "current salary" or "current variable compensation" — only answer
        # expected/desired salary (or truthful N/A / No when verified unemployed).
        is_current_var_comp = any(
            k in lower
            for k in (
                "variable compensation",
                "currently receive",
                "annual bonus",
                "stock options",
                "rsu",
                "remuneração variável",
                "remuneracao variavel",
                "bônus atual",
                "bonus atual",
            )
        )
        if is_current_var_comp:
            if self._profile.currently_employed is False:
                is_en = any(w in lower for w in ("currently", "compensation", "bonus", "receive"))
                if field.options:
                    return self._pick_best_option(
                        field.options,
                        preferred=("no", "não", "nao", "none", "n/a"),
                        fallback="No" if is_en else "Não",
                    )
                return "No (Not currently employed)" if is_en else "Não (Atualmente desempregado)"
            if field.required:
                raise HumanInterventionRequired(
                    f"Remuneração variável atual não informada no perfil ({field.label or field.name}).",
                    category="unverified_field",
                )
            return None

        is_current_salary = any(
            k in lower
            for k in ("current base salary", "current salary", "salário atual", "salario atual", "current compensation")
        )
        if is_current_salary:
            if self._profile.currently_employed is False:
                is_en = any(w in lower for w in ("current", "salary", "compensation", "base"))
                if field.field_type == "number":
                    return "0"
                return "N/A (Not currently employed)" if is_en else "N/A (Atualmente desempregado)"
            if field.required:
                raise HumanInterventionRequired(
                    f"Salário atual não informado no perfil verificado ({field.label or field.name}).",
                    category="unverified_field",
                )
            return None
        if any(
            k in lower
            for k in (
                "salary",
                "salário",
                "salario",
                "pretensão",
                "pretensao",
                "compensation",
                "remuneração",
                "remuneracao",
                "expectativa salarial",
            )
        ):
            sal_ans = self._answer_salary(field, job=job)
            if sal_ans:
                return sal_ans

        # Deterministic verified mappings
        direct = self._deterministic_answer(field, lower, job=job)
        if direct:
            return direct

        # Optional unknown fields can be safely left blank
        if not field.required:
            return None

        # Required non-standard question -> ask AI strictly using verified facts
        if not self._allow_ai:
            raise HumanInterventionRequired(
                f"Campo obrigatório sem mapeamento verificado: {field.label or field.name}",
                category="unverified_field",
            )

        answer, confident, reason = self._ask_ai_verified(field, job=job, resume_excerpt=resume_excerpt)
        if not confident or not answer:
            raise HumanInterventionRequired(
                f"Pergunta obrigatória não pôde ser respondida com segurança ({field.label or field.name}): {reason}",
                category="ambiguous_question",
            )
        return answer

    def _answer_salary(self, field: FormField, *, job: dict) -> str:
        p = self._profile
        lower = f"{field.label} {field.name}".lower()
        if not (p.salary_min or p.salary_max or p.salary_expectation):
            return ""

        contract_type = "unspecified"
        if "pj" in lower:
            contract_type = "pj"
        elif "clt" in lower:
            contract_type = "clt"

        if p.salary_min or p.salary_max:
            est = self._salary_estimator.estimate(job, contract_type=contract_type)
            return self._salary_estimator.format_for_field(
                est.amount_brl,
                field_label=field.label,
                placeholder=field.placeholder,
                field_type=field.field_type,
            )
        return p.salary_expectation

    def _deterministic_answer(self, field: FormField, lower: str, *, job: dict | None = None) -> str:
        p = self._profile
        field_type = field.field_type
        job = job or {}

        if field_type == "email" or any(k in lower for k in ("e-mail", "email", "confirmemail", "confirm_email")):
            return p.email
        if any(k in lower for k in ("country phone code", "código do país", "codigo do pais", "country code", "ddi")):
            return self._pick_best_option(
                field.options,
                preferred=("bra (+55)", "brasil (+55)", "brazil (+55)", "+55", "br"),
                fallback="Brasil (+55)",
            )
        if field_type == "tel" or any(k in lower for k in ("phone", "telefone", "celular", "whatsapp", "mobile")):
            return self._format_phone(p.phone, lower, field.placeholder)
        if any(k in lower for k in ("first name", "primeiro nome", "nome próprio", "first_name", "fname")):
            return p.first_name
        if any(k in lower for k in ("last name", "sobrenome", "last_name", "lname", "surname")):
            return p.last_name
        if (
            any(k in lower for k in ("full name", "nome completo", "your name", "seu nome", "nome_candidato", "_systemfield_name"))
            or field.label.strip().lower().rstrip("* ") in ("name", "nome")
            or field.name.strip().lower() in ("name", "nome")
        ):
            return p.full_name
        if "linkedin" in lower:
            return p.linkedin_url
        if "github" in lower:
            return p.github_url
        if any(k in lower for k in ("portfolio", "portfólio", "website", "site pessoal")):
            return p.portfolio_url or p.github_url

        # Conditional "If not, please describe..." optional follow-up -> leave blank
        if not field.required and any(k in lower for k in ("if not,", "se não,", "se nao,", "caso não", "caso nao")):
            return ""

        # Citizen / permanent resident yes-no question (must come BEFORE generic "country"/"based in" check!)
        if any(
            k in lower
            for k in (
                "citizen or permanent resident",
                "permanent resident",
                "cidadão ou residente",
                "cidadao ou residente",
                "are you a citizen",
            )
        ):
            job_loc = f"{job.get('title') or ''} {job.get('location') or ''}".lower()
            is_brazil_job = any(b in job_loc for b in ("brazil", "brasil", "br")) or (
                not job_loc.strip() and "bras" in (p.country or "").lower()
            )
            if is_brazil_job and any(b in (p.country or "").lower() for b in ("brazil", "brasil")):
                is_en = "citizen" in lower or "resident" in lower
                return self._pick_best_option(
                    field.options,
                    preferred=("yes", "sim"),
                    fallback="Yes" if is_en else "Sim",
                )
            return ""

        # How did you hear about this opportunity? (factual source of discovery)
        if any(
            k in lower
            for k in (
                "how did you hear",
                "hear about this",
                "como ficou sabendo",
                "como soube da vaga",
                "como conheceu",
                "onde encontrou esta vaga",
                "onde encontrou essa vaga",
            )
        ):
            src = (job.get("source") or "").lower()
            src_map = {
                "linkedin": "LinkedIn",
                "geekhunter": "GeekHunter",
                "nerdin": "Nerdin",
                "programathor": "Programathor",
                "querovagas": "QueroVagas",
                "devagas": "Devagas",
            }
            is_en = "how did" in lower or "hear about" in lower
            label_src = src_map.get(src, "Company Careers Page" if is_en else "Site de Carreiras da Empresa")
            if field.options:
                prefs = (
                    (label_src.lower(), "job board", "career", "website", "company", "other", "outro")
                    if src not in src_map
                    else (label_src.lower(), "job board", "social", "other", "outro")
                )
                return self._pick_best_option(field.options, preferred=prefs, fallback=label_src)
            return label_src

        # City / UF / Location / Country
        if any(k in lower for k in ("cidade-uf", "cidade/uf", "cidade - uf", "cidade_candidato")):
            if p.city and p.state:
                return f"{p.city}-{p.state}"
            return p.city or p.location
        if any(k in lower for k in ("city", "cidade")):
            return p.city or p.location
        if any(k in lower for k in ("country", "país", "pais", "based in")):
            return p.country
        if any(k in lower for k in ("location", "localização", "localizacao", "endereço", "address")):
            return p.location

        # CNPJ / Empresa aberta (boolean / select)
        if any(k in lower for k in ("empresa aberta", "possui_empresa", "possui cnpj", "tem cnpj")):
            if p.has_cnpj is True:
                return self._pick_best_option(
                    field.options,
                    preferred=("mais de 30", "30 dias", "sim", "yes", "possuo"),
                    fallback="Sim",
                )
            if p.has_cnpj is False:
                return self._pick_best_option(field.options, preferred=("não", "nao", "no"), fallback="Não")

        # Availability / Notice period (days or text)
        if any(k in lower for k in ("disponibilidade_inicio_dias", "dias para inicio", "dias para início")):
            return str(p.availability_days if p.availability_days is not None else 0)
        if any(k in lower for k in ("notice period", "disponibilidade", "start date", "availability")):
            if field_type == "number" or "dias" in lower or "days" in lower:
                return str(p.availability_days if p.availability_days is not None else 0)
            return p.availability

        # English level (verified C1 / Avançado from resumes)
        if any(k in lower for k in ("nivel_ingles", "nível de inglês", "nivel de ingles", "english level", "ingles", "inglês")):
            english_fact = next((l for l in p.languages if "ingl" in l.lower() or "english" in l.lower()), "")
            if english_fact:
                return self._pick_best_option(
                    field.options,
                    preferred=("avançado", "avancado", "c1", "advanced", "fluente", "fluent"),
                    fallback=english_fact,
                )

        # Current employment status & current employer (truthful verified answer).
        # Check open "where / current company" BEFORE boolean "currently employed" because
        # forms often phrase it as "If you're currently employed, where are you currently working?".
        is_en_label = any(
            w in lower
            for w in ("where ", "currently", "current ", "employer", "company", "employment", "working")
        )
        if any(
            k in lower
            for k in (
                "where are you currently working",
                "currently working",
                "current company",
                "current employer",
                "empresa atual",
                "empregador atual",
                "onde você trabalha",
                "onde voce trabalha",
                "onde trabalha atualmente",
            )
        ) or field.name.strip().lower() in ("org", "current_company", "current_employer"):
            if p.currently_employed is False:
                fallback = "Not currently employed" if is_en_label else "Atualmente desempregado"
                return self._pick_best_option(
                    field.options,
                    preferred=("unemployed", "not employed", "none", "n/a", "desempregado", "nenhuma", "sem vínculo", "sem vinculo"),
                    fallback=fallback,
                )
            if p.currently_employed is True and p.current_company:
                return p.current_company

        if any(
            k in lower
            for k in (
                "are you currently employed",
                "currently employed",
                "empregado atualmente",
                "atualmente empregado",
                "trabalhando atualmente",
            )
        ):
            if p.currently_employed is False:
                return self._pick_best_option(
                    field.options,
                    preferred=("não", "nao", "no"),
                    fallback="No" if is_en_label else "Não",
                )
            if p.currently_employed is True:
                return self._pick_best_option(
                    field.options,
                    preferred=("sim", "yes"),
                    fallback="Yes" if is_en_label else "Sim",
                )

        if any(
            k in lower
            for k in (
                "employment status",
                "status de emprego",
                "situação profissional",
                "situacao profissional",
                "situação atual",
                "situacao atual",
            )
        ):
            if p.currently_employed is False:
                return self._pick_best_option(
                    field.options,
                    preferred=("unemployed", "not employed", "seeking", "available", "desempregado", "buscando", "disponivel"),
                    fallback="Unemployed" if is_en_label else "Desempregado",
                )
            if p.currently_employed is True:
                return self._pick_best_option(
                    field.options,
                    preferred=("employed", "empregado"),
                    fallback="Employed" if is_en_label else "Empregado",
                )

        # Current function / role title
        if any(k in lower for k in ("funcao_candidato", "função atual", "funcao atual", "cargo atual", "current role")):
            return "Desenvolvedor Fullstack"

        # Work authorization
        if any(k in lower for k in ("work authorization", "autorização de trabalho", "authorized to work")):
            if field.options:
                return self._pick_best_option(field.options, preferred=("sim", "yes"), fallback=p.work_authorization)
            return p.work_authorization

        return ""

    @staticmethod
    def _format_phone(raw_phone: str, lower: str, placeholder: str = "") -> str:
        if not raw_phone:
            return ""
        digits = _digits_only(raw_phone)
        # Brazilian DDD inputs (e.g. GeekHunter/Nerdin "Celular com DDD" or "+55" already prefixed by country selector)
        if any(k in lower for k in ("ddd", "celular", "telefone_candidato")) or "99999" in (placeholder or ""):
            if digits.startswith("55") and len(digits) in (12, 13):
                local = digits[2:]
                return f"({local[:2]}) {local[2:7]}-{local[7:]}" if len(local) == 11 else local
        return raw_phone

    @staticmethod
    def _pick_best_option(options: list[str], *, preferred: tuple[str, ...], fallback: str) -> str:
        if not options:
            return fallback
        for kw in preferred:
            nkw = _norm(kw)
            for opt in options:
                if nkw in _norm(opt):
                    return opt
        return fallback

    def _ask_ai_verified(
        self,
        field: FormField,
        *,
        job: dict,
        resume_excerpt: str,
    ) -> tuple[str, bool, str]:
        prompt = f"{FORM_QA_SYSTEM}\n\n" + FORM_QA_USER.format(
            profile_facts=self._profile.as_verified_facts_text()[:3000],
            resume_excerpt=resume_excerpt[:3000],
            company=job.get("company") or "—",
            title=job.get("title") or "—",
            label=field.label or field.name,
            field_type=field.field_type,
            required="Sim" if field.required else "Não",
            options=", ".join(field.options) if field.options else "(campo aberto)",
        )
        try:
            raw = agy_ask(prompt, json_schema=FORM_QA_SCHEMA, timeout=60, max_retries=1)
            data = extract_json_object(raw) or {}
            return (
                str(data.get("answer") or "").strip(),
                bool(data.get("confident")),
                str(data.get("reason") or "informação não comprovada").strip(),
            )
        except Exception as exc:  # noqa: BLE001
            return "", False, f"erro na consulta de verificação: {exc}"


class PlaywrightFormFiller:
    """Inspects, fills, validates, and submits single- or multi-step web forms via Playwright."""

    def __init__(
        self,
        profile: CandidateProfile,
        *,
        allow_ai_qa: bool = True,
        allow_salary_web: bool = True,
    ):
        self._profile = profile
        self._answerer = VerifiedFactAnswerer(
            profile, allow_ai=allow_ai_qa, allow_salary_web=allow_salary_web
        )

    def run_on_page(
        self,
        page: Any,
        *,
        platform: str,
        apply_url: str,
        job: dict,
        documents: ApplicationDocuments,
        dry_run: bool = True,
        timeout_seconds: int = 60,
        cover_letter_provider: CoverLetterCallback | None = None,
        scope_selector: str | None = None,
        max_steps: int = 8,
    ) -> ApplicationOutcome:
        """Inspect page, guard against blockers, fill verified fields (multi-step aware), validate, and submit."""
        current_url = page.url or apply_url
        body_text = page.inner_text("body") if page.locator("body").count() else ""

        # 1. Hard compliance/security checks before touching any fields
        for check in (
            detect_active_captcha_on_page(page),
            detect_mfa_or_auth_wall(current_url, body_text),
            detect_recommendation_letter_request(body_text),
        ):
            if check:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=platform,
                    apply_url=current_url,
                    reason=check,
                    dry_run=dry_run,
                )

        resume_excerpt = ""
        if documents.resume_md and Path(documents.resume_md).exists():
            resume_excerpt = Path(documents.resume_md).read_text(encoding="utf-8")[:3500]

        filled_fields: dict[str, str] = {}
        cover_letter_requested = False
        cover_pdf = documents.cover_letter_pdf
        cover_text = documents.cover_letter_text
        any_field_found = False

        for step in range(1, max_steps + 1):
            current_url = page.url or apply_url
            raw_fields = self._extract_fields(page, scope_selector=scope_selector)
            if not raw_fields and not any_field_found:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=platform,
                    apply_url=current_url,
                    reason="Formulário de candidatura não encontrado ou carregado dinamicamente fora do padrão.",
                    dry_run=dry_run,
                )
            if raw_fields:
                any_field_found = True

            # Lazy cover letter check per step
            if any(is_cover_letter_label(f"{f.label} {f.name}") for f in raw_fields):
                cover_letter_requested = True
                if not cover_pdf and cover_letter_provider is not None:
                    cover_pdf, cover_text = cover_letter_provider()

            missing_required: list[str] = []
            try:
                for fld in raw_fields:
                    key_label = fld.label or fld.name or fld.selector
                    if fld.field_type == "file":
                        lower = f"{fld.label} {fld.name}".lower()
                        if detect_recommendation_letter_request(lower):
                            raise HumanInterventionRequired(
                                "Upload de carta de recomendação de terceiros solicitado.",
                                category="recommendation_letter",
                            )
                        if is_cover_letter_label(lower):
                            if cover_pdf and Path(cover_pdf).exists():
                                page.locator(fld.selector).first.set_input_files(cover_pdf)
                                filled_fields[key_label] = f"[FILE] {Path(cover_pdf).name}"
                            elif fld.required:
                                missing_required.append(key_label)
                        else:
                            if documents.resume_pdf and Path(documents.resume_pdf).exists():
                                page.locator(fld.selector).first.set_input_files(documents.resume_pdf)
                                filled_fields[key_label] = f"[FILE] {Path(documents.resume_pdf).name}"
                            elif fld.required:
                                missing_required.append(key_label)
                        continue

                    answer = self._answerer.answer_field(
                        fld,
                        job=job,
                        resume_excerpt=resume_excerpt,
                        cover_letter_text=cover_text,
                    )
                    if answer is None or answer == "":
                        if fld.required:
                            missing_required.append(key_label)
                        continue

                    self._fill_control(page, fld, answer)
                    filled_fields[key_label] = answer[:120]

            except HumanInterventionRequired as exc:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=platform,
                    apply_url=current_url,
                    reason=exc.reason,
                    cover_letter_requested=cover_letter_requested,
                    filled_fields=filled_fields,
                    dry_run=dry_run,
                )

            if missing_required:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=platform,
                    apply_url=current_url,
                    reason=f"Campos obrigatórios não preenchidos com dados verificados: {', '.join(missing_required[:5])}",
                    cover_letter_requested=cover_letter_requested,
                    filled_fields=filled_fields,
                    dry_run=dry_run,
                )

            # Check whether this is an intermediate wizard step (Next/Próximo/Avançar/Revisar)
            # Only advance when NO final submit button is currently visible.
            has_submit = self._find_submit_button(page, scope_selector=scope_selector) is not None
            if not has_submit and step < max_steps:
                next_btn = self._find_next_button(page, scope_selector=scope_selector)
                if next_btn is not None:
                    try:
                        next_btn.click(timeout=5000)
                        page.wait_for_timeout(1200)
                    except Exception as exc:  # noqa: BLE001
                        return ApplicationOutcome(
                            status="pending_human_action",
                            platform=platform,
                            apply_url=current_url,
                            reason=f"Falha ao avançar etapa {step} do formulário: {exc}",
                            cover_letter_requested=cover_letter_requested,
                            filled_fields=filled_fields,
                            dry_run=dry_run,
                        )
                    cap_step = detect_active_captcha_on_page(page)
                    if cap_step:
                        return ApplicationOutcome(
                            status="pending_human_action",
                            platform=platform,
                            apply_url=page.url or current_url,
                            reason=cap_step,
                            cover_letter_requested=cover_letter_requested,
                            filled_fields=filled_fields,
                            dry_run=dry_run,
                        )
                    continue
            break

        if not filled_fields:
            return ApplicationOutcome(
                status="pending_human_action",
                platform=platform,
                apply_url=page.url or current_url,
                reason="Nenhum campo de candidatura foi reconhecido ou preenchido na página.",
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=dry_run,
            )

        # Dry-run stops cleanly right before clicking Submit
        if dry_run:
            return ApplicationOutcome(
                status="documents_ready",
                platform=platform,
                apply_url=page.url or current_url,
                reason=f"[DRY-RUN] Formulário preenchido e validado ({len(filled_fields)} campos), envio não executado.",
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=True,
            )

        # Final Submit & demand explicit platform confirmation
        submit_btn = self._find_submit_button(page, scope_selector=scope_selector)
        if submit_btn is None:
            return ApplicationOutcome(
                status="pending_human_action",
                platform=platform,
                apply_url=page.url or current_url,
                reason="Botão de envio da candidatura não identificado com segurança.",
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=False,
            )

        # If the submit button is still disabled after filling, wait briefly for client-side validation
        # and escalate cleanly to pending_human_action instead of timing out across retries.
        try:
            if submit_btn.is_disabled():
                page.wait_for_timeout(500)
                if submit_btn.is_disabled():
                    return ApplicationOutcome(
                        status="pending_human_action",
                        platform=platform,
                        apply_url=page.url or current_url,
                        reason="Botão de envio permaneceu desabilitado após preenchimento do formulário. Verifique manualmente.",
                        cover_letter_requested=cover_letter_requested,
                        filled_fields=filled_fields,
                        dry_run=False,
                    )
        except Exception:  # noqa: BLE001
            pass

        try:
            submit_btn.click(timeout=8000)
        except Exception as exc:  # noqa: BLE001
            return ApplicationOutcome(
                status="application_failed",
                platform=platform,
                apply_url=page.url or current_url,
                reason=f"Erro ao clicar no botão de envio: {exc}",
                retryable=True,
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=False,
            )

        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_seconds * 1000, 12000))
        except Exception:  # noqa: BLE001
            page.wait_for_timeout(3000)

        post_url = page.url or current_url
        post_text = page.inner_text("body") if page.locator("body").count() else ""

        captcha_after = detect_active_captcha_on_page(page)
        if captcha_after:
            return ApplicationOutcome(
                status="pending_human_action",
                platform=platform,
                apply_url=post_url,
                reason=f"Desafio pós-envio detectado: {captcha_after}",
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=False,
            )

        confirmed, conf_id = detect_submission_confirmation(post_url, post_text)
        if confirmed:
            return ApplicationOutcome(
                status="applied",
                platform=platform,
                apply_url=post_url,
                confirmation_id=conf_id,
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=False,
            )

        # Check if visible validation errors remained on screen
        err_match = _ERROR_TEXT_RE.search(post_text[:4000])
        detail = f" Erro de validação visível: '{err_match.group(0)}'." if err_match else ""
        return ApplicationOutcome(
            status="pending_human_action",
            platform=platform,
            apply_url=post_url,
            reason=f"Envio acionado, mas a plataforma não exibiu confirmação explícita.{detail} Verifique manualmente.",
            cover_letter_requested=cover_letter_requested,
            filled_fields=filled_fields,
            dry_run=False,
        )

    @staticmethod
    def _extract_fields(page: Any, *, scope_selector: str | None = None) -> list[FormField]:
        js = """(scopeSel) => {
            const IGNORE_NOISE = /(newsletter|cookie|consent|analytics|ads|search|busca|pesquisa|password|senha|login|signin|g-recaptcha-response|h-captcha-response|cf-turnstile-response)/i;
            document.querySelectorAll('[data-jobbot-field]').forEach(e => e.removeAttribute('data-jobbot-field'));
            let root = document;
            if (scopeSel) {
                const scoped = document.querySelector(scopeSel);
                if (scoped) root = scoped;
            }
            // Prefer inputs inside a non-newsletter/non-search <form>, fallback to all inputs in root (Ashby/React SPAs)
            let candidates = [];
            const forms = Array.from(root.querySelectorAll('form')).filter(f => {
                const meta = ((f.id || '') + ' ' + (f.className || '') + ' ' + (f.action || '')).toLowerCase();
                if (/(newsletter|search|login|signin|subscribe)/i.test(meta)) return false;
                const nonHidden = f.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select');
                return nonHidden.length >= 2 || f.querySelector('input[type="file"]');
            });
            if (forms.length > 0) {
                for (const f of forms) {
                    candidates.push(...Array.from(f.querySelectorAll('input, textarea, select')));
                }
            } else {
                candidates = Array.from(root.querySelectorAll('input, textarea, select'));
            }

            const out = [];
            let autoIdx = 0;
            for (const el of candidates) {
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || tag).toLowerCase();
                if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
                if (el.disabled) continue;

                // Exclude header/nav/footer/cookie-banner inputs unless inside a chosen modal scope
                if (!scopeSel) {
                    if (el.closest('header, nav, footer')) continue;
                    const banner = el.closest('[id*="cookie" i], [class*="cookie" i], [id*="consent" i]');
                    if (banner && banner !== el && !(type === 'checkbox' && el.closest('form'))) {
                        continue;
                    }
                }

                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                if (type !== 'file') {
                    if (
                        style.display === 'none' ||
                        style.visibility === 'hidden' ||
                        parseFloat(style.opacity || '1') === 0 ||
                        el.getAttribute('aria-hidden') === 'true' ||
                        rect.width <= 1 ||
                        rect.height <= 1
                    ) {
                        continue;
                    }
                } else {
                    // Skip hidden file inputs inside display:none ancestors (e.g. inactive wizard step)
                    let p = el.parentElement;
                    let inactiveStep = false;
                    while (p && p !== document.body) {
                        const ps = window.getComputedStyle(p);
                        if (ps.display === 'none' || ps.visibility === 'hidden') {
                            // Allow common dropzone wrapper where only input or immediate wrapper is sr-only,
                            // skip if ancestor is clearly a hidden step/pane/modal
                            if ((p.className || '').toString().match(/step|pane|tab|modal|d-none|hidden/i)) {
                                inactiveStep = true;
                                break;
                            }
                        }
                        p = p.parentElement;
                    }
                    if (inactiveStep) continue;
                }

                let label = '';
                if (el.id) {
                    const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    if (lbl) label = lbl.innerText.trim();
                }
                if (!label) {
                    const parentLabel = el.closest('label');
                    if (parentLabel) label = parentLabel.innerText.trim();
                }
                if (!label && el.parentElement) {
                    // Check preceding sibling label or container label (Chakra/React/Ashby)
                    const group = el.closest('[role="group"], .form-group, .field, .mb-3, .chakra-form-control');
                    if (group) {
                        const gl = group.querySelector('label');
                        if (gl) label = gl.innerText.trim();
                    }
                }
                if (!label) {
                    const ariaBy = el.getAttribute('aria-labelledby') || '';
                    if (ariaBy) {
                        label = ariaBy.split(/\\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
                    }
                }
                const placeholder = el.getAttribute('placeholder') || '';
                if (!label) {
                    label = el.getAttribute('aria-label') || placeholder || el.name || '';
                }

                const noiseCheck = `${el.id || ''} ${el.name || ''} ${label}`;
                if (type !== 'file' && IGNORE_NOISE.test(noiseCheck)) {
                    // Allow privacy/terms/LGPD consent checkbox even if id/name/label contains "consent" or "cookies"
                    if (!(type === 'checkbox' && /(privacidade|privacy|termos|terms|lgpd|tratamento dos meus dados|concordo)/i.test(label))) {
                        continue;
                    }
                }

                const required = Boolean(
                    el.required ||
                    el.getAttribute('aria-required') === 'true' ||
                    label.includes('*')
                );
                const options = tag === 'select'
                    ? Array.from(el.options).map(o => o.text.trim()).filter(Boolean)
                    : [];

                // Assign stable unique data attribute so selector never collides
                autoIdx += 1;
                const marker = `jb-fld-${autoIdx}`;
                el.setAttribute('data-jobbot-field', marker);
                const selector = `[data-jobbot-field="${marker}"]`;

                const role = (el.getAttribute('role') || '').toLowerCase();
                const resolvedType = tag === 'textarea'
                    ? 'textarea'
                    : (tag === 'select' ? 'select' : (role === 'combobox' ? 'combobox' : type));

                out.push({
                    name: el.name || el.id || '',
                    label: label.replace(/\\s+/g, ' ').trim(),
                    field_type: resolvedType,
                    required,
                    selector,
                    options,
                    placeholder,
                });
            }
            return out;
        }"""
        try:
            items = page.evaluate(js, scope_selector) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("DOM form extraction failed: %s", exc)
            return []
        return [FormField(**item) for item in items]

    @staticmethod
    def _fill_control(page: Any, fld: FormField, answer: str) -> None:
        loc = page.locator(fld.selector).first
        if fld.field_type == "select":
            # Try exact label, then value, then case-insensitive / normalized substring match
            try:
                loc.select_option(label=answer)
                return
            except Exception:  # noqa: BLE001
                pass
            try:
                loc.select_option(value=answer)
                return
            except Exception:  # noqa: BLE001
                pass
            nans = _norm(answer)
            for opt in fld.options:
                nopt = _norm(opt)
                if nans and (nans == nopt or nans in nopt or nopt in nans):
                    loc.select_option(label=opt)
                    return
            # Fallback: try matching by index if option exists
            raise HumanInterventionRequired(
                f"Opção '{answer}' não encontrada no campo select '{fld.label or fld.name}' (opções: {', '.join(fld.options[:5])}).",
                category="select_mismatch",
            )
        elif fld.field_type == "combobox":
            loc.click(timeout=3000)
            loc.fill(answer)
            page.wait_for_timeout(650)
            nans = _norm(answer)
            chosen = False
            for opt_sel in (
                "[id*='react-select'][role='option']",
                "[role='listbox'] [role='option']",
                "[role='option']",
            ):
                opts = page.locator(opt_sel)
                cnt = min(opts.count(), 25)
                if cnt == 0:
                    continue
                first_visible = None
                for i in range(cnt):
                    o = opts.nth(i)
                    try:
                        if not o.is_visible(timeout=200):
                            continue
                        if first_visible is None:
                            first_visible = o
                        otxt = _norm(o.inner_text(timeout=300) or "")
                        if nans and (nans == otxt or nans in otxt or otxt in nans):
                            o.click(timeout=2000)
                            chosen = True
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if chosen:
                    break
                if first_visible is not None and opt_sel.startswith("[id*='react-select']"):
                    try:
                        first_visible.click(timeout=2000)
                        chosen = True
                        break
                    except Exception:  # noqa: BLE001
                        pass
            if not chosen:
                try:
                    loc.press("Enter")
                except Exception:  # noqa: BLE001
                    pass
        elif fld.field_type == "checkbox":
            if answer.lower() in ("true", "yes", "sim", "1"):
                try:
                    loc.check(force=True)
                except Exception:  # noqa: BLE001
                    loc.click(force=True)
        elif fld.field_type == "radio":
            loc.check(force=True)
        else:
            loc.click(timeout=3000)
            loc.fill(answer)
            # Trigger blur so React/masked currency & phone inputs commit validation state
            try:
                loc.dispatch_event("change")
                loc.dispatch_event("blur")
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _iter_buttons(page: Any, scope_selector: str | None = None):
        prefix = f"{scope_selector} " if scope_selector else ""
        # Check form buttons first, then general buttons (for React/Ashby without <form>)
        locator = page.locator(
            f"{prefix}form button, {prefix}form input[type='submit'], {prefix}button[type='submit'], {prefix}button"
        )
        count = min(locator.count(), 30)
        for i in range(count):
            yield locator.nth(i)

    @classmethod
    def _find_submit_button(cls, page: Any, *, scope_selector: str | None = None):
        fallback_submit = None
        for btn in cls._iter_buttons(page, scope_selector=scope_selector):
            try:
                if not btn.is_visible(timeout=300):
                    continue
                txt = (btn.inner_text(timeout=300) or btn.get_attribute("value") or "").strip()
                txt_clean = re.sub(r"\s+", " ", txt)
                btn_type = (btn.get_attribute("type") or "").lower()
                if _NEXT_STEP_BUTTON_RE.match(txt_clean):
                    continue
                if _SUBMIT_BUTTON_RE.match(txt_clean):
                    return btn
                if btn_type == "submit" and fallback_submit is None:
                    # Exclude search/newsletter submit buttons
                    if not re.search(r"search|busca|pesquis|inscrev|newsletter|entrar|login", txt_clean, re.I):
                        fallback_submit = btn
            except Exception:  # noqa: BLE001
                continue
        return fallback_submit

    @classmethod
    def _find_next_button(cls, page: Any, *, scope_selector: str | None = None):
        for btn in cls._iter_buttons(page, scope_selector=scope_selector):
            try:
                if not btn.is_visible(timeout=300):
                    continue
                txt = re.sub(r"\s+", " ", (btn.inner_text(timeout=300) or btn.get_attribute("value") or "").strip())
                if _NEXT_STEP_BUTTON_RE.match(txt):
                    return btn
            except Exception:  # noqa: BLE001
                continue
        return None
