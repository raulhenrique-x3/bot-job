"""Shared verified-fact answerer and multi-step Playwright form inspection/filling engine."""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

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
    r"^(submit|submit application|submit your application|apply|apply now|send application|send your application|send|"
    r"enviar|enviar candidatura|enviar candidatura simplificada|finalizar candidatura|concluir candidatura|"
    r"candidatar-se|candidatar para a vaga|finalizar|confirmar candidatura)$",
    re.IGNORECASE,
)

_NEXT_STEP_BUTTON_RE = re.compile(
    r"^(next|continue|continue to next step|review|review your application|save and continue|"
    r"avancar|avançar|proximo|próximo|continuar|continuar para a pr[oó]xima etapa|"
    r"revisar|revisar candidatura|revisar sua candidatura|"
    r"avaliar|avaliar candidatura|avaliar sua candidatura|"
    r"pr[oó]xima etapa|salvar e continuar)$",
    re.IGNORECASE,
)

_ERROR_TEXT_RE = re.compile(
    r"(campo obrigat[óo]rio|preencha este campo|required field|is required|"
    r"invalid email|e-mail inv[áa]lido|telefone inv[áa]lido|formato inv[áa]lido|"
    r"please enter|please select|please make a selection|fa[çc]a uma sele[çc][aã]o|"
    r"insira uma resposta v[áa]lida|enter a valid|voc[êe] precisa aceitar)",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).strip()


def _digits_only(text: str) -> str:
    return re.sub(r"\D+", "", text or "")


_HUMAN_ANSWER_PROVIDER: Callable[[str | None], dict[str, str]] | None = None


def set_human_answer_provider(provider: Callable[[str | None], dict[str, str]] | None) -> None:
    """Register callable(job_id) -> {question: answer} for candidate-confirmed answers."""
    global _HUMAN_ANSWER_PROVIDER
    _HUMAN_ANSWER_PROVIDER = provider


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
        # Candidate-confirmed answers take priority: the candidate is the authority for their own facts.
        human = self._human_answer_for(field, job=job)
        if human is not None:
            return human
        label_and_name = f"{field.label} {field.name}".strip()
        lower = label_and_name.lower()

        # Standard Privacy Policy / Terms of Use checkbox or radio (e.g. GeekHunter / LinkedIn JOIN ATS consent)
        if field.field_type == "checkbox" and is_privacy_terms_checkbox(label_and_name):
            return "true"
        if field.field_type == "radio" and is_privacy_terms_checkbox(label_and_name):
            is_en = any(w in lower for w in ("privacy", "terms", "agree", "policy"))
            return self._pick_best_option(
                field.options,
                preferred=("yes", "sim", "agree", "aceito", "concordo"),
                fallback="Yes" if is_en else "Sim",
            )

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

    def _human_answer_for(self, field: FormField, *, job: dict | None) -> str | None:
        """Return a candidate-confirmed answer for this field, or None if none was recorded."""
        provider = _HUMAN_ANSWER_PROVIDER
        if provider is None:
            return None
        job_id = job.get("id") if isinstance(job, dict) else None
        try:
            answers = provider(job_id) or {}
        except Exception:  # noqa: BLE001
            return None
        if not answers:
            return None
        target = _norm(f"{field.label} {field.name}")
        best, best_score = "", 0.0
        for question, answer in answers.items():
            qn = _norm(question)
            if qn == target:
                best, best_score = answer, 1.0
                break
            score = difflib.SequenceMatcher(None, qn, target).ratio()
            if score > best_score:
                best, best_score = answer, score
        if not best or best_score < 0.5:
            return None
        if field.field_type in ("checkbox", "radio", "select", "combobox"):
            # Confirmed consent/selection: check or choose the agreed option.
            return "sim"
        return best

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
            if any(k in lower for k in ("device type", "phone type", "tipo de telefone", "tipo de dispositivo")):
                is_en = "device" in lower or "phone" in lower
                return self._pick_best_option(
                    field.options,
                    preferred=("mobile", "cell", "celular"),
                    fallback="Mobile" if is_en else "Celular",
                )
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
                "ficou sabendo da vaga",
                "ficou sabendo das",
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
                "avanade": "Avanade Career Site",
                "inhire": "Página de Carreiras",
                "remotive": "Remotive",
                "himalayas": "Himalayas",
                "weworkremotely": "We Work Remotely",
                "jobicy": "Jobicy",
                "workingnomads": "Working Nomads",
                "wellfound": "Wellfound",
            }
            is_en = "how did" in lower or "hear about" in lower
            label_src = src_map.get(src, "Company Careers Page" if is_en else "Site de Carreiras da Empresa")
            if field.options:
                prefs = (
                    (label_src.lower(), "avanade", "career", "company", "website", "job board", "other", "outro")
                    if src in ("avanade",) or src not in src_map
                    else (label_src.lower(), "job board", "social", "other", "outro")
                )
                return self._pick_best_option(field.options, preferred=prefs, fallback=label_src)
            return label_src

        # Previously worked for company (Workday / Avanade / Accenture standard question)
        if any(
            k in lower
            for k in (
                "previously worked for",
                "have you ever worked for",
                "former employee",
                "já trabalhou na",
                "ja trabalhou na",
                "trabalhou anteriormente",
            )
        ):
            is_en = any(w in lower for w in ("previously", "worked", "former", "employee"))
            return self._pick_best_option(
                field.options,
                preferred=("no", "não", "nao"),
                fallback="No" if is_en else "Não",
            )

        # Technology-specific experience (e.g. profile.skill_experience_years = {"Java": "1"})
        if p.skill_experience_years:
            for tech, years_val in p.skill_experience_years.items():
                # Word-boundary match so "Java" never matches "JavaScript"
                if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(tech.lower())}(?![A-Za-z0-9_])", lower):
                    continue
                is_years_q = (
                    field_type == "number"
                    or any(
                        k in lower
                        for k in (
                            "how many years",
                            "years of experience",
                            "years experience",
                            "quantos anos",
                            "anos de experiência",
                            "anos de experiencia",
                            "tempo de experiência",
                            "tempo de experiencia",
                        )
                    )
                )
                if is_years_q:
                    years_digits = _digits_only(years_val) or str(years_val).strip()
                    if field.options:
                        return self._pick_best_option(
                            field.options,
                            preferred=(
                                f"{years_digits} year",
                                f"{years_digits} ano",
                                f"{years_digits}-",
                                f"{years_digits} -",
                                f"{years_digits} a ",
                                f"less than {int(years_digits) + 1}" if years_digits.isdigit() else years_digits,
                                years_digits,
                            ),
                            fallback=years_digits,
                        )
                    return years_digits
                if any(
                    k in lower
                    for k in (
                        "do you have experience",
                        "have experience",
                        "possui experiência",
                        "possui experiencia",
                        "tem experiência",
                        "tem experiencia",
                        "conhecimento em",
                        "proficiency",
                    )
                ):
                    is_en = any(w in lower for w in ("do you", "have experience", "proficiency"))
                    return self._pick_best_option(
                        field.options,
                        preferred=("yes", "sim", "1 year", "1 ano", "basic", "básico", "basico", "intermediate"),
                        fallback="Yes" if is_en else "Sim",
                    )

        # Yes/No Brazil residence or remote work questions (must precede generic "based in" -> country)
        if any(
            k in lower
            for k in (
                "based in brazil",
                "based in brasil",
                "located in brazil",
                "located in brasil",
                "live in brazil",
                "reside no brasil",
                "mora no brasil",
            )
        ):
            if any(b in (p.country or "").lower() for b in ("brazil", "brasil")):
                is_en = any(w in lower for w in ("based", "located", "live", "brazil"))
                return self._pick_best_option(
                    field.options,
                    preferred=("yes", "sim"),
                    fallback="Yes" if is_en else "Sim",
                )

        if any(
            k in lower
            for k in (
                "remote setting",
                "working remotely",
                "work remotely",
                "trabalhar remotamente",
                "trabalho remoto",
            )
        ):
            is_en = any(w in lower for w in ("remote", "working", "work"))
            return self._pick_best_option(
                field.options,
                preferred=("yes", "sim"),
                fallback="Yes" if is_en else "Sim",
            )

        # General professional software engineering years (when not matched by a specific skill above)
        if p.years_of_experience and any(
            k in lower
            for k in (
                "years of professional experience",
                "years of experience in software",
                "years of software engineering",
                "anos de experiência em desenvolvimento",
                "anos de experiencia em desenvolvimento",
                "anos de experiência como desenvolvedor",
            )
        ):
            yrs = _digits_only(p.years_of_experience) or p.years_of_experience.strip()
            if field.options:
                return self._pick_best_option(
                    field.options,
                    preferred=(f"{yrs} year", f"{yrs} ano", f"{yrs}+", f"{yrs}-", yrs),
                    fallback=yrs,
                )
            return yrs

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

        # Availability / Notice period (days, weeks, or text)
        if any(k in lower for k in ("disponibilidade_inicio_dias", "dias para inicio", "dias para início")):
            return str(p.availability_days if p.availability_days is not None else 0)
        if any(
            k in lower
            for k in (
                "notice period",
                "disponibilidade",
                "start date",
                "availability",
                "how soon can you join",
                "when can you start",
            )
        ):
            days_val = p.availability_days if p.availability_days is not None else 0
            if "week" in lower or "semana" in lower:
                return str(max(0, days_val // 7))
            if field_type == "number" or "dias" in lower or "days" in lower:
                return str(days_val)
            return p.availability

        # English level (verified C1 / Avançado from resumes)
        if any(
            k in lower
            for k in (
                "nivel_ingles",
                "nível de inglês",
                "nivel de ingles",
                "english level",
                "english proficiency",
                "proficiency in english",
                "ingles",
                "inglês",
            )
        ):
            english_fact = next((l for l in p.languages if "ingl" in l.lower() or "english" in l.lower()), "")
            if english_fact:
                return self._pick_best_option(
                    field.options,
                    preferred=(
                        "avançado",
                        "avancado",
                        "c1",
                        "advanced",
                        "professional",
                        "profissional",
                        "fluente",
                        "fluent",
                    ),
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

        try:
            url_blob = f"{current_url} {apply_url}".lower()
            if "jobicy.com" in url_blob and page.locator("#jv-open-apply-auth").count() > 0:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=platform,
                    apply_url=apply_url if current_url == "about:blank" else current_url,
                    reason="Portal agregador (Jobicy) exige login/cadastro próprio para revelar o link externo da vaga.",
                    dry_run=dry_run,
                )
        except Exception:  # noqa: BLE001
            pass

        resume_excerpt = ""
        if documents.resume_md and Path(documents.resume_md).exists():
            resume_excerpt = Path(documents.resume_md).read_text(encoding="utf-8")[:3500]

        filled_fields: dict[str, str] = {}
        cover_letter_requested = False
        cover_pdf = documents.cover_letter_pdf
        cover_text = documents.cover_letter_text
        any_field_found = False
        resume_uploaded_via_btn = False

        for step in range(1, max_steps + 1):
            current_url = page.url or apply_url
            raw_fields = self._extract_fields(page, scope_selector=scope_selector)
            if not raw_fields and not any_field_found and hasattr(page, "wait_for_timeout"):
                for _ in range(5):
                    page.wait_for_timeout(500)
                    raw_fields = self._extract_fields(page, scope_selector=scope_selector)
                    if raw_fields:
                        break
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

            # LinkedIn 2026 SDUI resume upload button (uses JS filechooser with no <input type="file"> in DOM)
            if (
                not resume_uploaded_via_btn
                and documents.resume_pdf
                and Path(documents.resume_pdf).exists()
                and not any(f.field_type == "file" for f in raw_fields)
            ):
                if self._try_upload_via_button(page, scope_selector, documents.resume_pdf):
                    resume_uploaded_via_btn = True
                    any_field_found = True
                    filled_fields["Currículo"] = f"[FILE] {Path(documents.resume_pdf).name}"

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

            # Check whether this is an intermediate wizard step (Next/Próximo/Avançar/Revisar/Avaliar)
            # Only advance when NO final submit button is currently visible.
            has_submit = self._find_submit_button(page, scope_selector=scope_selector) is not None
            if not has_submit and step < max_steps:
                next_btn = self._find_next_button(page, scope_selector=scope_selector)
                if next_btn is not None:
                    before_sig = self._step_signature(page, scope_selector=scope_selector)
                    try:
                        try:
                            next_btn.scroll_into_view_if_needed(timeout=2000)
                        except Exception:  # noqa: BLE001
                            pass
                        next_btn.click(timeout=5000)
                        page.wait_for_timeout(900)
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
                    after_sig = self._step_signature(page, scope_selector=scope_selector)
                    if before_sig and after_sig == before_sig:
                        page.wait_for_timeout(900)
                        after_sig = self._step_signature(page, scope_selector=scope_selector)
                    if before_sig and after_sig == before_sig:
                        val_err = self._visible_validation_error(page, scope_selector=scope_selector)
                        detail = f": {val_err}" if val_err else " (verifique campos obrigatórios na etapa)."
                        return ApplicationOutcome(
                            status="pending_human_action",
                            platform=platform,
                            apply_url=page.url or current_url,
                            reason=f"Etapa {step} do formulário não avançou após clicar em continuar{detail}",
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

        # Dry-run stops cleanly right before clicking Submit — still verify Submit button is reachable
        submit_btn = self._find_submit_button(page, scope_selector=scope_selector)
        if dry_run:
            if submit_btn is None:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=platform,
                    apply_url=page.url or current_url,
                    reason="[DRY-RUN] Formulário preenchido, mas botão final de envio não foi alcançado.",
                    cover_letter_requested=cover_letter_requested,
                    filled_fields=filled_fields,
                    dry_run=True,
                )
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
            try:
                submit_btn.scroll_into_view_if_needed(timeout=2000)
            except Exception:  # noqa: BLE001
                pass
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

        # Poll for explicit confirmation / validation error instead of blocking on `networkidle`
        # (SPAs like LinkedIn stream background telemetry and never reach networkidle).
        post_url = page.url or current_url
        post_text = ""
        confirmed = False
        conf_id = ""
        max_wait_ms = min(timeout_seconds * 1000, 7000)
        waited_ms = 0
        while waited_ms < max_wait_ms:
            page.wait_for_timeout(500)
            waited_ms += 500
            try:
                post_url = page.url or current_url
                post_text = page.inner_text("body") if page.locator("body").count() else ""
            except Exception:  # noqa: BLE001
                continue
            confirmed, conf_id = detect_submission_confirmation(post_url, post_text)
            if confirmed:
                break
            if self._visible_validation_error(page, scope_selector=scope_selector):
                break

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
        val_err = self._visible_validation_error(page, scope_selector=scope_selector)
        err_match = _ERROR_TEXT_RE.search(post_text[:4000])
        detail = f" Erro de validação visível: '{val_err or err_match.group(0)}'." if (val_err or err_match) else ""
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
        js = r"""(scopeSel) => {
            const IGNORE_NOISE = /(newsletter|cookie|consent|analytics|ads|search|busca|pesquisa|password|senha|login|signin|beecatcher|robots only|g-recaptcha-response|h-captcha-response|cf-turnstile-response|global_resume_upload|resume_upload_inner_block|banner-special)/i;
            document.querySelectorAll('[data-jobbot-field]').forEach(e => e.removeAttribute('data-jobbot-field'));
            document.querySelectorAll('[data-jobbot-radio-group]').forEach(e => {
                e.removeAttribute('data-jobbot-radio-group');
                e.removeAttribute('data-jobbot-radio-opt');
            });
            let root = document;
            if (scopeSel) {
                const matches = Array.from(document.querySelectorAll(scopeSel));
                const visibleMatch = matches.find(m => {
                    const s = window.getComputedStyle(m);
                    const r = m.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 50 && r.height > 50;
                });
                if (visibleMatch) root = visibleMatch;
                else if (matches[0]) root = matches[0];
            }
            // Prefer inputs inside a non-newsletter/non-search <form>, fallback to all inputs in root (Ashby/React/LinkedIn SDUI)
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
            const handledRadioGroups = new Set();

            const radioOptionText = (r) => {
                if (r.id) {
                    const lbl = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
                    if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
                }
                const parentLbl = r.closest('label');
                if (parentLbl && parentLbl.innerText.trim()) return parentLbl.innerText.trim();
                // LinkedIn 2026 SDUI: <div><input type="radio"><label for=...></label></div><div><p>Yes</p></div>
                const p = r.parentElement;
                if (p && p.nextElementSibling && p.nextElementSibling.innerText.trim()) {
                    return p.nextElementSibling.innerText.trim();
                }
                if (p && p.parentElement && p.parentElement.innerText.trim()) {
                    return p.parentElement.innerText.trim();
                }
                return r.value && r.value !== 'on' ? r.value : '';
            };

            for (const el of candidates) {
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || tag).toLowerCase();
                if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
                if (el.disabled) continue;

                // Exclude header/nav/footer/cookie-banner/sidebar-promo inputs unless inside a chosen modal scope
                if (!scopeSel) {
                    if (el.closest('header, nav, footer')) continue;
                    if (el.closest('#resume_upload_inner_block, .banner-special, aside') && !el.closest('form')) {
                        continue;
                    }
                    const banner = el.closest('[id*="cookie" i], [class*="cookie" i], [id*="consent" i]');
                    if (banner && banner !== el && !(type === 'checkbox' && el.closest('form'))) {
                        continue;
                    }
                }

                // Group radio buttons by name (supports LinkedIn SDUI visually-hidden sr-only radios inside visible fieldsets)
                if (type === 'radio' && el.name) {
                    if (handledRadioGroups.has(el.name)) continue;
                    handledRadioGroups.add(el.name);
                    const groupEls = candidates.filter(c => c.tagName.toLowerCase() === 'input' && (c.getAttribute('type') || '').toLowerCase() === 'radio' && c.name === el.name && !c.disabled);
                    const container = el.closest('fieldset, [role="radiogroup"]') || el.parentElement?.parentElement || el.parentElement;
                    if (!container) continue;
                    const cs = window.getComputedStyle(container);
                    const cr = container.getBoundingClientRect();
                    if (cs.display === 'none' || cs.visibility === 'hidden' || cr.width <= 1 || cr.height <= 1) continue;

                    const firstAria = (el.getAttribute('aria-label') || '').trim();
                    // Skip LinkedIn's saved-resume radio selector (already handled via upload or pre-selected)
                    if (/\.(pdf|docx?)$/i.test(firstAria)) continue;

                    let qLabel = firstAria;
                    if (!qLabel) {
                        const ariaBy = el.getAttribute('aria-labelledby') || container.getAttribute('aria-labelledby') || '';
                        if (ariaBy) {
                            qLabel = ariaBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
                        }
                    }
                    if (!qLabel) {
                        const legend = container.querySelector('legend');
                        if (legend) qLabel = legend.innerText.trim();
                    }
                    if (!qLabel) {
                        qLabel = (container.getAttribute('aria-label') || '').trim();
                    }
                    if (!qLabel) continue;
                    if (IGNORE_NOISE.test(`${el.name} ${qLabel}`)) {
                        if (!/(privacidade|privacy|termos|terms|lgpd|tratamento dos meus dados|concordo)/i.test(qLabel)) {
                            continue;
                        }
                    }

                    const opts = [];
                    autoIdx += 1;
                    const marker = `jb-fld-${autoIdx}`;
                    for (const r of groupEls) {
                        const optTxt = radioOptionText(r).replace(/\s+/g, ' ').trim();
                        if (!optTxt) continue;
                        r.setAttribute('data-jobbot-radio-group', marker);
                        r.setAttribute('data-jobbot-radio-opt', optTxt);
                        opts.push(optTxt);
                    }
                    if (opts.length === 0) continue;
                    el.setAttribute('data-jobbot-field', marker);
                    const anyChecked = groupEls.some(r => r.checked);
                    const req = Boolean(
                        groupEls.some(r => r.required || r.getAttribute('aria-required') === 'true') ||
                        qLabel.includes('*') ||
                        (!anyChecked && container.hasAttribute('aria-describedby'))
                    );
                    out.push({
                        name: el.name,
                        label: qLabel.replace(/\s+/g, ' ').trim(),
                        field_type: 'radio',
                        required: req,
                        selector: `[data-jobbot-field="${marker}"]`,
                        options: opts,
                        placeholder: '',
                    });
                    continue;
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
                        label = ariaBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
                    }
                }
                const placeholder = el.getAttribute('placeholder') || '';
                if (!label) {
                    label = el.getAttribute('aria-label') || placeholder || el.name || '';
                }

                const noiseCheck = `${el.id || ''} ${el.name || ''} ${el.getAttribute('data-automation-id') || ''} ${label}`;
                if (IGNORE_NOISE.test(noiseCheck)) {
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
                    label: label.replace(/\s+/g, ' ').trim(),
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
    def _try_upload_via_button(page: Any, scope_selector: str | None, resume_pdf: str) -> bool:
        """Upload resume PDF via LinkedIn SDUI 'Carregar currículo' / 'Upload resume' button filechooser."""
        if not hasattr(page, "expect_file_chooser") or not hasattr(page, "evaluate"):
            return False
        try:
            found = page.evaluate(
                r"""(scopeSel) => {
                    document.querySelectorAll('[data-jobbot-upload-btn]').forEach(e => e.removeAttribute('data-jobbot-upload-btn'));
                    let root = document;
                    if (scopeSel) {
                        const ms = Array.from(document.querySelectorAll(scopeSel));
                        root = ms.find(m => {
                            const s = getComputedStyle(m);
                            const r = m.getBoundingClientRect();
                            return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 50 && r.height > 50;
                        }) || ms[0] || document;
                    }
                    const btns = Array.from(root.querySelectorAll('button'));
                    for (const b of btns) {
                        const s = getComputedStyle(b);
                        const r = b.getBoundingClientRect();
                        if (s.display === 'none' || s.visibility === 'hidden' || r.width < 10 || r.height < 10) continue;
                        const txt = (b.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase();
                        if (txt === 'carregar currículo' || txt === 'carregar curriculo' || txt === 'upload resume') {
                            b.setAttribute('data-jobbot-upload-btn', '1');
                            return true;
                        }
                    }
                    return false;
                }""",
                scope_selector,
            )
            if not found:
                return False
            btn = page.locator("[data-jobbot-upload-btn='1']").first
            with page.expect_file_chooser(timeout=2500) as fc_info:
                btn.click(timeout=3000)
            fc_info.value.set_files(resume_pdf)
            page.wait_for_timeout(1200)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Resume button filechooser upload skipped: %s", exc)
            return False

    @staticmethod
    def _step_signature(page: Any, *, scope_selector: str | None = None) -> str:
        if not hasattr(page, "evaluate"):
            return ""
        try:
            return str(
                page.evaluate(
                    r"""(scopeSel) => {
                        let root = document;
                        if (scopeSel) {
                            const ms = Array.from(document.querySelectorAll(scopeSel));
                            root = ms.find(m => {
                                const s = getComputedStyle(m);
                                const r = m.getBoundingClientRect();
                                return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 50 && r.height > 50;
                            }) || ms[0] || document;
                        }
                        const textNode = root === document ? document.body : root;
                        const txt = ((textNode && textNode.innerText) || '').replace(/\s+/g, ' ').trim().slice(0, 260);
                        const visCnt = Array.from(root.querySelectorAll('input, select, textarea, button')).filter(el => {
                            const s = getComputedStyle(el);
                            const r = el.getBoundingClientRect();
                            return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 1 && r.height > 1;
                        }).length;
                        return `${location.href}|${visCnt}|${txt}`;
                    }""",
                    scope_selector,
                )
                or ""
            )
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _visible_validation_error(page: Any, *, scope_selector: str | None = None) -> str:
        if not hasattr(page, "evaluate"):
            return ""
        try:
            return str(
                page.evaluate(
                    r"""(scopeSel) => {
                        let root = document;
                        if (scopeSel) {
                            const ms = Array.from(document.querySelectorAll(scopeSel));
                            root = ms.find(m => {
                                const s = getComputedStyle(m);
                                const r = m.getBoundingClientRect();
                                return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 50 && r.height > 50;
                            }) || ms[0] || document;
                        }
                        const errs = root.querySelectorAll('[role="alert"], .artdeco-inline-feedback--error, [id^="error-message-"], [class*="error-message" i]');
                        for (const el of errs) {
                            const s = getComputedStyle(el);
                            const r = el.getBoundingClientRect();
                            if (s.display === 'none' || s.visibility === 'hidden' || r.width < 2 || r.height < 2) continue;
                            const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
                            if (txt) return txt.slice(0, 160);
                        }
                        return '';
                    }""",
                    scope_selector,
                )
                or ""
            )
        except Exception:  # noqa: BLE001
            return ""

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
            marker_match = re.search(r'data-jobbot-field="([^"]+)"', fld.selector)
            if marker_match and fld.options and hasattr(page, "evaluate"):
                marker = marker_match.group(1)
                nans = _norm(answer)
                # Prefer exact match, then substring match among group options
                chosen_idx = -1
                for idx, opt in enumerate(fld.options):
                    if _norm(opt) == nans:
                        chosen_idx = idx
                        break
                if chosen_idx < 0:
                    for idx, opt in enumerate(fld.options):
                        nopt = _norm(opt)
                        if nans and (nans in nopt or nopt in nans):
                            chosen_idx = idx
                            break
                if chosen_idx < 0:
                    raise HumanInterventionRequired(
                        f"Opção '{answer}' não encontrada no grupo de opções '{fld.label or fld.name}' (opções: {', '.join(fld.options[:5])}).",
                        category="select_mismatch",
                    )
                target_radio = page.locator(f'[data-jobbot-radio-group="{marker}"]').nth(chosen_idx)
                try:
                    target_radio.check(force=True, timeout=3000)
                    return
                except Exception:  # noqa: BLE001
                    pass
                # Fallback: click associated <label for> or visual wrapper
                page.evaluate(
                    """([m, idx]) => {
                        const radios = document.querySelectorAll(`[data-jobbot-radio-group="${m}"]`);
                        const r = radios[idx];
                        if (!r) return;
                        const lbl = r.id ? document.querySelector(`label[for="${CSS.escape(r.id)}"]`) : null;
                        if (lbl) lbl.click();
                        else r.click();
                    }""",
                    [marker, chosen_idx],
                )
                return
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
        targets = ("form button", "form input[type='submit']", "button[type='submit']", "button")
        if scope_selector:
            # Scope safely even when scope_selector is a comma-separated CSS selector list
            # (naive string prefixing breaks CSS precedence: "a, b button" != "a button, b button").
            root = page.locator(scope_selector).first
            if hasattr(root, "locator"):
                locator = root.locator(", ".join(targets))
            else:
                parts = [s.strip() for s in scope_selector.split(",") if s.strip()]
                combined = ", ".join(f"{p} {t}" for p in parts for t in targets)
                locator = page.locator(combined)
        else:
            locator = page.locator(", ".join(targets))
        count = min(locator.count(), 30)
        for i in range(count):
            yield locator.nth(i)

    @classmethod
    def _find_action_button_fast(cls, page: Any, kind: str, scope_selector: str | None = None):
        """Single-evaluate button finder for real Playwright pages (no 30-button cap or IPC thrashing)."""
        js = r"""(scopeSel) => {
            document.querySelectorAll('[data-jobbot-btn-idx]').forEach(e => e.removeAttribute('data-jobbot-btn-idx'));
            let root = document;
            if (scopeSel) {
                const ms = Array.from(document.querySelectorAll(scopeSel));
                const vis = ms.find(m => {
                    const s = getComputedStyle(m);
                    const r = m.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 50 && r.height > 50;
                });
                if (vis) root = vis;
                else if (ms[0]) root = ms[0];
            }
            const els = Array.from(root.querySelectorAll('form button, form input[type="submit"], button[type="submit"], button'));
            const seen = new Set();
            const out = [];
            let idx = 0;
            for (const el of els) {
                if (seen.has(el)) continue;
                seen.add(el);
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') === 0 || r.width < 2 || r.height < 2) {
                    continue;
                }
                idx += 1;
                el.setAttribute('data-jobbot-btn-idx', String(idx));
                out.push({
                    idx,
                    text: (el.innerText || el.value || '').replace(/\s+/g, ' ').trim(),
                    aria: (el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim(),
                    type: (el.getAttribute('type') || '').toLowerCase(),
                    isSubmitAttr: el.hasAttribute('data-live-test-easy-apply-submit-button'),
                    isNextAttr: el.hasAttribute('data-easy-apply-next-button'),
                });
            }
            return out;
        }"""
        try:
            buttons = page.evaluate(js, scope_selector) or []
        except Exception:  # noqa: BLE001
            return None

        fallback_idx: int | None = None
        for b in buttons:
            txt = b.get("text") or ""
            aria = b.get("aria") or ""
            btn_type = b.get("type") or ""
            if kind == "submit":
                if b.get("isSubmitAttr"):
                    return page.locator(f'[data-jobbot-btn-idx="{b["idx"]}"]').first
                if _NEXT_STEP_BUTTON_RE.match(txt) or (not txt and _NEXT_STEP_BUTTON_RE.match(aria)):
                    continue
                if _SUBMIT_BUTTON_RE.match(txt) or (aria and _SUBMIT_BUTTON_RE.match(aria)):
                    return page.locator(f'[data-jobbot-btn-idx="{b["idx"]}"]').first
                if btn_type == "submit" and fallback_idx is None:
                    if not re.search(r"search|busca|pesquis|inscrev|newsletter|entrar|login", f"{txt} {aria}", re.I):
                        fallback_idx = b["idx"]
            else:
                if b.get("isNextAttr"):
                    return page.locator(f'[data-jobbot-btn-idx="{b["idx"]}"]').first
                if _NEXT_STEP_BUTTON_RE.match(txt) or (aria and _NEXT_STEP_BUTTON_RE.match(aria)):
                    return page.locator(f'[data-jobbot-btn-idx="{b["idx"]}"]').first

        if kind == "submit" and fallback_idx is not None:
            return page.locator(f'[data-jobbot-btn-idx="{fallback_idx}"]').first
        return None

    @classmethod
    def _find_submit_button(cls, page: Any, *, scope_selector: str | None = None):
        if hasattr(page, "evaluate"):
            return cls._find_action_button_fast(page, "submit", scope_selector=scope_selector)
        fallback_submit = None
        for btn in cls._iter_buttons(page, scope_selector=scope_selector):
            try:
                if not btn.is_visible(timeout=300):
                    continue
                txt = (btn.inner_text(timeout=300) or btn.get_attribute("value") or "").strip()
                txt_clean = re.sub(r"\s+", " ", txt)
                aria_clean = re.sub(r"\s+", " ", (btn.get_attribute("aria-label") or "").strip())
                btn_type = (btn.get_attribute("type") or "").lower()
                if _NEXT_STEP_BUTTON_RE.match(txt_clean):
                    continue
                if _SUBMIT_BUTTON_RE.match(txt_clean) or (aria_clean and _SUBMIT_BUTTON_RE.match(aria_clean)):
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
        if hasattr(page, "evaluate"):
            return cls._find_action_button_fast(page, "next", scope_selector=scope_selector)
        for btn in cls._iter_buttons(page, scope_selector=scope_selector):
            try:
                if not btn.is_visible(timeout=300):
                    continue
                txt = re.sub(r"\s+", " ", (btn.inner_text(timeout=300) or btn.get_attribute("value") or "").strip())
                aria = re.sub(r"\s+", " ", (btn.get_attribute("aria-label") or "").strip())
                if _NEXT_STEP_BUTTON_RE.match(txt) or (aria and _NEXT_STEP_BUTTON_RE.match(aria)):
                    return btn
            except Exception:  # noqa: BLE001
                continue
        return None
