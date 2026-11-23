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
    detect_cloudflare_block,
    detect_mfa_or_auth_wall,
    detect_recommendation_letter_request,
    detect_sensitive_or_legal_field,
    detect_submission_confirmation,
    is_cover_letter_label,
    is_cpf_only_field,
    is_privacy_terms_checkbox,
)
from bot.appliers.dom_ai import (
    ask_ai_dom_recovery,
    dismiss_blocking_overlays,
    resolve_iframe_target,
    resolve_locator,
    try_dismiss_overlay,
    validate_ai_fields,
)
from bot.appliers.salary import SalaryBudgetEstimator
from bot.gemini.client import agy_ask, agy_available, extract_json_object
from bot.gemini.prompts import FORM_QA_SYSTEM, FORM_QA_USER
from bot.profile import CandidateProfile, smart_truncate

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


_PREVIOUSLY_WORKED_RE = re.compile(
    r"(?:previously\s+(?:been\s+)?(?:worked|employed|contracted)|"
    r"have\s+you\s+(?:ever|previously)\s+(?:worked|been\s+employed|been\s+contracted)|"
    r"employed\s+or\s+contracted\s+by|"
    r"worked\s+or\s+contracted\s+(?:for|by|with)|"
    r"former\s+employee|subsidiar(?:y|ies)|"
    r"j[áa]\s+(?:trabalhou|atuou|prestou\s+servi[çc]o|foi\s+(?:empregad[oa]|contratad[oa]))|"
    r"trabalhou\s+anteriormente|ex-?funcion[áa]ri[oa])",
    re.IGNORECASE,
)

_FORM_EXPIRED_RE = re.compile(
    r"(?:vaga\s+(?:encerrada|expirada|indispon[ií]vel|n[ãa]o\s+encontrada)|"
    r"inscri[cç][õo]es\s+encerradas|n[ãa]o\s+aceita\s+mais\s+candidaturas|"
    r"no\s+longer\s+accepting\s+applications|position\s+has\s+been\s+filled|"
    r"job\s+(?:is\s+closed|not\s+found|has\s+expired)|page\s+not\s+found)",
    re.IGNORECASE,
)

_APPLY_CTA_BUTTON_RE = re.compile(
    r"^(?:apply|apply\s+now|apply\s+for\s+this(?:\s+(?:job|position|role))?|"
    r"start\s+application|candidatar-?se|quero\s+me\s+candidatar|inscrever-?se|"
    r"enviar\s+curr[ií]culo)$",
    re.IGNORECASE,
)

_NOISE_FRAME_RE = re.compile(
    r"(?:recaptcha|hcaptcha|turnstile|challenges\.cloudflare|doubleclick|googletagmanager|"
    r"google-analytics|handtalk|clarity\.ms|facebook|hotjar|intercom|crisp)",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).strip()


def _digits_only(text: str) -> str:
    return re.sub(r"\D+", "", text or "")


def _parse_years_float(raw: str) -> float | None:
    """Parse an experience string like '4+', '4.5', '1' into a numeric year value.

    A trailing '+' (e.g. '4+') is modeled as base + 0.4 so it sits cleanly inside
    ranges like '3 a 5 anos' and '4+ years', and closer to '2 a 4 anos' than '5 anos ou mais'
    when a form leaves an integer gap between 4 and 5.
    """
    txt = (raw or "").strip().replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", txt)
    if not m:
        return None
    val = float(m.group(1))
    if "+" in txt or "mais" in txt.lower() or "over" in txt.lower() or "more" in txt.lower():
        val += 0.4
    return val


def _parse_years_range(option: str) -> tuple[float, float] | None:
    """Parse a human-readable years-of-experience option into (low, high) years."""
    low_txt = _norm(option)
    if not low_txt:
        return None
    nums = [float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", low_txt)]
    if not nums:
        if any(k in low_txt for k in ("nenhum", "sem experiencia", "no experience", "none")):
            return (0.0, 0.0)
        return None

    # Explicit range: "2 a 4 anos", "3-5 years", "1 to 3", "de 2 ate 4"
    if len(nums) >= 2 and re.search(r"\d+\s*(?:-|–|—|a|ate|to)\s*\d+", low_txt):
        lo, hi = min(nums[0], nums[1]), max(nums[0], nums[1])
        return (lo, hi)

    n = nums[0]
    if any(k in low_txt for k in ("menos de", "less than", "under", "below", "ate ", "up to", "<")):
        return (0.0, n)
    if any(k in low_txt for k in ("acima de", "mais de", "more than", "over", ">")):
        return (n + 0.01, 99.0)
    if any(k in low_txt for k in ("ou mais", "or more", "at least", "pelo menos", "+")):
        return (n, 99.0)
    return (n, n)


def _pick_years_range_option(options: list[str], years: float) -> str:
    """Select the best matching years-of-experience option for a numeric year count."""
    if not options:
        return str(int(years))

    parsed: list[tuple[str, float, float]] = []
    for opt in options:
        rng = _parse_years_range(opt)
        if rng is not None:
            parsed.append((opt, rng[0], rng[1]))
    if not parsed:
        return ""

    # 1. Exact containment (prefer the tightest containing bucket)
    containing = [(opt, lo, hi) for opt, lo, hi in parsed if lo <= years <= hi]
    if containing:
        containing.sort(key=lambda item: (item[2] - item[1], abs(((item[1] + min(item[2], item[1] + 5.0)) / 2.0) - years)))
        return containing[0][0]

    # 2. Closest bucket when value falls in an integer gap (e.g. 4.4 between [2, 4] and [5, 99])
    def _dist(item: tuple[str, float, float]) -> tuple[float, float]:
        _, lo, hi = item
        if years < lo:
            return (lo - years, hi - lo)
        return (years - hi, hi - lo)

    parsed.sort(key=_dist)
    return parsed[0][0]


_GENERAL_EXP_DURATION_RE = re.compile(
    r"(?:quanto tempo de experi[eê]ncia|tempo de experi[eê]ncia|"
    r"quantos anos.*experi[eê]ncia|anos de experi[eê]ncia|"
    r"years of (?:professional |work )?experience|years experience|"
    r"how many years.*experience|how long have you worked)",
    re.IGNORECASE,
)

_GENERAL_EXP_ROLE_RE = re.compile(
    r"\b(?:full[\s-]?stack|front[\s-]?end|back[\s-]?end|"
    r"software\s+(?:engineer(?:ing)?|develop(?:er|ment))|"
    r"engenheir[oa]\s+de\s+software|engenharia\s+de\s+software|"
    r"desenvolvedor(?:a)?|desenvolvimento(?:\s+(?:de\s+software|web))?|"
    r"web\s+develop(?:er|ment)|programador(?:a)?|programa[cç][aã]o|"
    r"years\s+of\s+professional\s+experience|experi[eê]ncia\s+profissional)\b",
    re.IGNORECASE,
)

_SPECIFIC_TECH_QUALIFIER_RE = re.compile(
    r"\b(?:with|using|com|em)\s+(?!(?:desenvolvimento|software|web|front|back|full|programa|ti\b|tecnologia|experi))([a-z0-9.#+_-]{2,})",
    re.IGNORECASE,
)


def _is_general_experience_question(lower: str) -> bool:
    if not _GENERAL_EXP_DURATION_RE.search(lower):
        return False
    if not _GENERAL_EXP_ROLE_RE.search(lower):
        return False
    # Never hijack questions asking about a specific tool/language (e.g. "as a backend dev with Go")
    if _SPECIFIC_TECH_QUALIFIER_RE.search(lower):
        return False
    return True


_BACKEND_TITLE_RE = re.compile(
    r"\b(?:back[\s-]?end|backend[\s-]?leaning|api\s+engineer|platform\s+engineer|"
    r"data\s+engineer|ml\s+engineer|ai\s+engineer|python\s+(?:engineer|developer)|"
    r"node\.?js\s+(?:engineer|developer)|\.net\s+(?:engineer|developer)|java\s+(?:engineer|developer)|"
    r"golang\s+(?:engineer|developer))\b",
    re.IGNORECASE,
)
_FRONTEND_TITLE_RE = re.compile(
    r"\b(?:front[\s-]?end|frontend[\s-]?leaning|ui\s+engineer|react\s+(?:engineer|developer)|"
    r"next\.?js\s+(?:engineer|developer))\b",
    re.IGNORECASE,
)
_EXPLICIT_BACKEND_BODY_RE = re.compile(
    r"(?:back[\s-]?end[\s-]?leaning|foco\s+(?:em|no)\s+back[\s-]?end|mais\s+voltad[ao]\s+para\s+back[\s-]?end|"
    r"[eê]nfase\s+(?:em|no)\s+back[\s-]?end|predominantemente\s+back[\s-]?end|heavy\s+back[\s-]?end|"
    r"stronger\s+(?:in|on)\s+back[\s-]?end|back[\s-]?end[\s-]?focus(?:ed)?|maior\s+peso\s+(?:em|no)\s+back[\s-]?end)",
    re.IGNORECASE,
)
_EXPLICIT_FRONTEND_BODY_RE = re.compile(
    r"(?:front[\s-]?end[\s-]?leaning|foco\s+(?:em|no)\s+front[\s-]?end|mais\s+voltad[ao]\s+para\s+front[\s-]?end|"
    r"[eê]nfase\s+(?:em|no)\s+front[\s-]?end|predominantemente\s+front[\s-]?end|heavy\s+front[\s-]?end|"
    r"stronger\s+(?:in|on)\s+front[\s-]?end|front[\s-]?end[\s-]?focus(?:ed)?|maior\s+peso\s+(?:em|no)\s+front[\s-]?end)",
    re.IGNORECASE,
)
_BACKEND_BODY_TERMS = (
    "backend", "back-end", "microsserviço", "microsservico", "microservices",
    "api rest", "restful", "graphql", "postgresql", "mysql", "sql server", "mongodb",
    "redis", "kafka", "rabbitmq", "mensageria", "worker", "pipeline", "server-side",
    "nestjs", "fastapi", "django", ".net", "spring", "golang", "kubernetes",
)
_FRONTEND_BODY_TERMS = (
    "frontend", "front-end", "react", "next.js", "nextjs", "vue", "angular",
    "tailwind", "scss", "css", "html", "ui/ux", "ux/ui", "design system",
    "acessibilidade", "accessibility", "a11y", "web vitals", "ssr", "microfront",
    "client-side", "lado do cliente", "interfaces de usuário", "interfaces de usuario",
)


def _infer_job_focus(job: dict | None) -> str:
    """Return 'backend' if the job explicitly demands/leans backend, else 'frontend' (candidate default)."""
    if not isinstance(job, dict):
        return "frontend"
    title = str(job.get("title") or "")
    body = str(job.get("body") or "")

    has_be_title = bool(_BACKEND_TITLE_RE.search(title))
    has_fe_title = bool(_FRONTEND_TITLE_RE.search(title))
    if has_be_title and not has_fe_title:
        return "backend"
    if has_fe_title and not has_be_title:
        return "frontend"

    if _EXPLICIT_BACKEND_BODY_RE.search(body) and not _EXPLICIT_FRONTEND_BODY_RE.search(body):
        return "backend"
    if _EXPLICIT_FRONTEND_BODY_RE.search(body) and not _EXPLICIT_BACKEND_BODY_RE.search(body):
        return "frontend"

    body_low = body.lower()
    be_hits = sum(1 for term in _BACKEND_BODY_TERMS if term in body_low)
    fe_hits = sum(1 for term in _FRONTEND_BODY_TERMS if term in body_low)
    if be_hits >= fe_hits + 3 and be_hits >= max(4, int(fe_hits * 1.5)):
        return "backend"
    return "frontend"


def _is_frontend_backend_question(field: FormField, lower: str) -> bool:
    """Detect subjective preference/identification questions between Frontend and Backend."""
    has_front_q = any(k in lower for k in ("frontend", "front-end", "front end", "front ou back", "front or back"))
    has_back_q = any(k in lower for k in ("backend", "back-end", "back end", "front ou back", "front or back"))
    if has_front_q and has_back_q:
        if any(
            k in lower
            for k in (
                "identifica",
                "prefere",
                "preferência",
                "preferencia",
                "foco",
                "focus",
                "lean",
                "mais forte",
                "stronger",
                "especialidade",
                "perfil",
                "área",
                "area",
                "ou back",
                "or back",
                "ou front",
                "or front",
                "vs",
            )
        ):
            return True

    if field.options and 2 <= len(field.options) <= 4:
        norm_opts = [_norm(o) for o in field.options]
        has_front_opt = any("front" in o for o in norm_opts)
        has_back_opt = any("back" in o for o in norm_opts)
        has_full_opt = any("full" in o for o in norm_opts)
        if has_front_opt and has_back_opt and not has_full_opt:
            return True
    return False


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
        # Candidate-confirmed CPF field (only when CPF is configured in CandidateProfile)
        if is_cpf_only_field(label_and_name) and self._profile.cpf:
            return _digits_only(self._profile.cpf) or self._profile.cpf.strip()

        # Standard conflict-of-interest / "nothing to declare" declarations
        is_conflict_decl = any(
            k in lower
            for k in (
                "nothing to declare",
                "nada a declarar",
                "conflict of interest",
                "conflicts of interest",
                "conflito de interesse",
                "conflitos de interesse",
                "outside business",
                "outside employment",
            )
        )

        # Candidate-confirmed demographic/EEO fields (disability, race/ethnicity, sexual orientation, gender identity)
        demo_ans = self._answer_demographic(field, lower)
        if demo_ans is not None:
            return demo_ans
        if not is_cnpj_boolean and not is_conflict_decl:
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

        is_current_salary = (
            not any(k in lower for k in ("expect", "desir", "pretens", "minimum", "will you accept"))
            and (
                any(
                    k in lower
                    for k in ("current base salary", "current salary", "salário atual", "salario atual", "current compensation")
                )
                or bool(re.search(r"\bcurrent\b.*\b(salary|compensation|pay)\b|\bremunera[çc][ãa]o atual\b", lower))
            )
        )
        if is_current_salary:
            if self._profile.currently_employed is False:
                is_en = any(w in lower for w in ("current", "salary", "compensation", "base"))
                is_numeric = (
                    field.field_type == "number"
                    or bool(re.search(r"\b(in usd|usd|ex:\s*\d|format:\s*\$)", f"{lower} {(field.placeholder or '').lower()}"))
                )
                if is_numeric:
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
        if not best or best_score < 0.82:
            return None
        if field.field_type in ("checkbox", "radio", "select", "combobox"):
            # If the stored answer is an explicit option value (e.g. "No", "Black", "Cisgender Man"), return it directly
            if best.strip().lower() not in ("", "sim", "yes", "true", "concordo", "aceito"):
                return best.strip()
            return "sim"
        return best

    def _answer_demographic(self, field: FormField, lower: str) -> str | None:
        """Answer demographic/EEO questions when explicitly configured by the candidate in CandidateProfile."""
        p = self._profile
        # 1. Disability / PcD
        if any(k in lower for k in ("disabilit", "deficiência", "deficiencia", "pcd", "handicap")):
            if p.has_disability is False:
                is_en = "disabilit" in lower or "do you" in lower
                return self._pick_best_option(
                    field.options,
                    preferred=("no", "não", "nao", "none", "nenhuma", "i do not have"),
                    fallback="No" if is_en else "Não",
                )
            if p.has_disability is True:
                is_en = "disabilit" in lower or "do you" in lower
                return self._pick_best_option(
                    field.options,
                    preferred=("yes", "sim"),
                    fallback="Yes" if is_en else "Sim",
                )

        # 2. Race / Ethnicity (use word boundaries so "cor" does not match "concorda"/"acordo" and "race" does not match "trace")
        if re.search(r"\b(racial|ethnic|ethnicity|race|raça|raca|cor|etnia)\b", lower):
            if p.race_ethnicity:
                return self._pick_best_option(
                    field.options,
                    preferred=("black", "preto", "preta", "negro", "negra", "hispanic or latino", "latino", "pardo", "afro"),
                    fallback="Black",
                )

        # 3. Sexual Orientation
        if any(k in lower for k in ("sexual orientation", "orientação sexual", "orientacao sexual")):
            if p.sexual_orientation:
                return self._pick_best_option(
                    field.options,
                    preferred=("heterosexual", "heterossexual", "straight", "hetero", "hétero"),
                    fallback="Heterosexual",
                )

        # 4. Gender Identity / Gender
        if any(k in lower for k in ("gender identity", "identidade de gênero", "identidade de genero", "which gender", "gênero", "genero")):
            if p.gender_identity:
                return self._pick_best_option(
                    field.options,
                    preferred=("cisgender man", "homem cis", "cis man", "male", "masculino", "man", "homem", "cisgender"),
                    fallback="Cisgender Man",
                )

        return None

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

        # How did you hear / find us? (factual source of discovery)
        if any(
            k in lower
            for k in (
                "how did you hear",
                "hear about this",
                "how did you find",
                "where did you find",
                "como ficou sabendo",
                "ficou sabendo da vaga",
                "ficou sabendo das",
                "como soube da vaga",
                "como conheceu",
                "como nos encontrou",
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
            is_en = "how did" in lower or "where did" in lower or "hear about" in lower
            label_src = src_map.get(src, "Company Careers Page" if is_en else "Site de Carreiras da Empresa")
            if field.options:
                if src == "linkedin":
                    prefs: tuple[str, ...] = ("linkedin job", "linkedin post", "linkedin", "job board", "social", "other", "outro")
                elif src in ("avanade",) or src not in src_map:
                    prefs = (label_src.lower(), "avanade", "career", "company", "website", "job board", "other", "outro")
                else:
                    prefs = (label_src.lower(), "job board", "social", "other", "outro")
                return self._pick_best_option(field.options, preferred=prefs, fallback=label_src)
            return label_src

        # Previously worked / employed / contracted by company or subsidiaries
        if _PREVIOUSLY_WORKED_RE.search(lower):
            company_norm = _norm(str(job.get("company") or ""))
            known_employers = ("bsatech", "bsa tech", "compurshop", "gerens")
            worked_there = bool(
                company_norm and any(emp in company_norm or company_norm in emp for emp in known_employers)
            )
            is_en = any(
                w in lower for w in ("previously", "worked", "employed", "contracted", "former", "subsidiar")
            )
            if worked_there:
                return self._pick_best_option(
                    field.options,
                    preferred=("yes", "sim"),
                    fallback="Yes" if is_en else "Sim",
                )
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
                        years_num = _parse_years_float(years_val)
                        if years_num is not None:
                            range_pick = _pick_years_range_option(field.options, years_num)
                            if range_pick:
                                return range_pick
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

        # Yes/No Brazil or Latin America residence questions (must precede generic "based in" -> country)
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
                "latin america",
                "américa latina",
                "america latina",
                "based in latam",
            )
        ):
            if any(b in (p.country or "").lower() for b in ("brazil", "brasil", "br")):
                is_en = any(w in lower for w in ("based", "located", "live", "brazil", "latin"))
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

        # Role preference ("Which role would you prefer among others today?") & Frontend vs Backend
        if any(k in lower for k in ("which role would you prefer", "role would you prefer", "qual função prefere", "qual funcao prefere")):
            title_low = str(job.get("title") or "").lower()
            if _BACKEND_TITLE_RE.search(title_low) and not _FRONTEND_TITLE_RE.search(title_low):
                return self._pick_best_option(field.options, preferred=("back-end", "backend", "full-stack", "fullstack"), fallback="Back-end")
            if _FRONTEND_TITLE_RE.search(title_low) and not _BACKEND_TITLE_RE.search(title_low):
                return self._pick_best_option(field.options, preferred=("front-end", "frontend", "full-stack", "fullstack"), fallback="Front-end")
            return self._pick_best_option(field.options, preferred=("full-stack", "fullstack", "front-end", "frontend"), fallback="Full-stack")

        # Frontend vs Backend identification / preference: align to job demand, default Front-end
        if _is_frontend_backend_question(field, lower):
            focus = _infer_job_focus(job)
            if focus == "backend":
                return self._pick_best_option(
                    field.options,
                    preferred=("back-end", "backend", "back"),
                    fallback="Back-end",
                )
            return self._pick_best_option(
                field.options,
                preferred=("front-end", "frontend", "front"),
                fallback="Front-end",
            )

        # General professional software engineering / Fullstack / Frontend / Backend years
        if p.years_of_experience and _is_general_experience_question(lower):
            yrs = _digits_only(p.years_of_experience) or p.years_of_experience.strip()
            if field.options:
                years_num = _parse_years_float(p.years_of_experience)
                if years_num is not None:
                    range_pick = _pick_years_range_option(field.options, years_num)
                    if range_pick:
                        return range_pick
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
            if field.options:
                loc_prefs = [x for x in (p.city, p.state, "pernambuco", "são paulo", "sao paulo") if x]
                picked_loc = self._pick_best_option(
                    field.options,
                    preferred=tuple(loc_prefs),
                    fallback="",
                )
                if picked_loc:
                    return picked_loc
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

        # Availability / Notice period (days, weeks, or text) — exclude education/experience start date years
        if any(k in lower for k in ("disponibilidade_inicio_dias", "dias para inicio", "dias para início")):
            return str(p.availability_days if p.availability_days is not None else 0)
        if "start-year" not in lower and "start date year" not in lower and "start_year" not in lower and any(
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

        # Spanish level (defaults to BASIC when configured or asked)
        if any(
            k in lower
            for k in (
                "nivel_espanhol",
                "nível de espanhol",
                "nivel de espanhol",
                "spanish level",
                "spanish proficiency",
                "proficiency in spanish",
                "espanhol",
                "spanish",
            )
        ):
            is_en = "spanish" in lower or "proficiency" in lower
            return self._pick_best_option(
                field.options,
                preferred=("basic", "básico", "basico", "beginner", "iniciante", "elementary", "a1", "a2", "limited"),
                fallback="BASIC" if is_en else "Básico",
            )

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
                "spoken english",
                "written english",
                "english language",
                "ingles",
                "inglês",
            )
        ):
            english_fact = next((l for l in p.languages if "ingl" in l.lower() or "english" in l.lower()), "")
            if english_fact:
                return self._pick_best_option(
                    field.options,
                    preferred=(
                        "advanced",
                        "avançado",
                        "avancado",
                        "c1",
                        "upper intermediate",
                        "upper-intermediate",
                        "professional",
                        "profissional",
                        "fluente",
                        "fluent",
                    ),
                    fallback="Advanced" if "english" in lower else english_fact,
                )

        # Current employment status & current employer (truthful verified answer).
        # Check open "where / current company" BEFORE boolean "currently employed" because
        # forms often phrase it as "If you're currently employed, where are you currently working?".
        is_en_label = any(
            w in lower
            for w in ("where ", "currently", "current ", "employer", "company", "employment", "working")
        )
        if (
            not any(k in lower for k in ("family member", "relative", "parente", "spouse", "friend"))
            and (
                any(
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
                )
                or field.name.strip().lower() in ("org", "current_company", "current_employer")
            )
        ):
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

        # Latin America residence question (e.g. "Are you currently based in Latin America?")
        if any(k in lower for k in ("latin america", "américa latina", "america latina", "latam")):
            if any(b in (p.country or "").lower() for b in ("brazil", "brasil", "br")):
                is_en = "latin" in lower or "based" in lower
                return self._pick_best_option(
                    field.options,
                    preferred=("yes", "sim"),
                    fallback="Yes" if is_en else "Sim",
                )

        # Portuguese fluency (verified native from Portuguese resumes & Brazil location)
        if any(k in lower for k in ("fluent in portuguese", "portuguese proficiency", "fluente em português", "fluente em portugues")):
            if any("portug" in l.lower() for l in p.languages):
                is_en = "fluent" in lower or "portuguese" in lower
                return self._pick_best_option(
                    field.options,
                    preferred=("yes", "sim", "native", "nativo", "fluent", "fluente"),
                    fallback="Yes" if is_en else "Sim",
                )

        # English functional communication declaration / comfort in English teams
        if any(
            k in lower
            for k in (
                "communicate functionally in english",
                "operate in english",
                "teams that primarily operate in english",
                "working in english",
                "comunicar em inglês",
                "comunicar em ingles",
            )
        ):
            if any("ingl" in l.lower() or "english" in l.lower() for l in p.languages):
                is_en = "english" in lower
                return self._pick_best_option(
                    field.options,
                    preferred=("yes", "sim", "i declare", "agree", "concordo"),
                    fallback="Yes" if is_en else "Sim",
                )

        # Data privacy / AI interview transcription / AI usage in take-home consent comboboxes
        if any(
            k in lower
            for k in (
                "on data privacy",
                "transcribed using artificial intelligence",
                "transcrição gerada",
                "we encourage ai on the technical work",
            )
        ):
            return self._pick_best_option(
                field.options,
                preferred=(
                    "yes, i agree",
                    "yes",
                    "sim",
                    "acknowledge",
                    "i agree",
                    "agree",
                    "concordo",
                    "aceito",
                    "understood",
                    "i understand",
                ),
                fallback="Yes",
            )

        # Special needs / accommodations in recruitment process
        if any(k in lower for k in ("special needs", "accommodate you during the recruitment", "necessidade especial")):
            return self._pick_best_option(
                field.options,
                preferred=("no", "none", "n/a", "não", "nao", "nenhuma"),
                fallback="No",
            )

        # Relatives at company / Public officers / Conflict of interest declarations
        if any(
            k in lower
            for k in (
                "family members currently working at",
                "relatives who are public officers",
                "parentes trabalhando",
                "agente público",
                "public officers",
            )
        ):
            return self._pick_best_option(
                field.options,
                preferred=("no", "none", "n/a", "não", "nao"),
                fallback="No",
            )
        if any(k in lower for k in ("nothing to declare", "nada a declarar")):
            if field_type == "checkbox":
                return "true"
            if field.options:
                return self._pick_best_option(
                    field.options,
                    preferred=("nothing to declare", "i have nothing to declare", "nada a declarar", "no", "none", "não", "nao"),
                    fallback="I have nothing to declare.",
                )
            return "I have nothing to declare."
        if any(
            k in lower
            for k in (
                "conflicts of interest",
                "conflict of interest",
                "conflito de interesse",
                "conflitos de interesse",
                "outside business",
                "outside employment",
            )
        ):
            if field.options:
                return self._pick_best_option(
                    field.options,
                    preferred=("nothing to declare", "i have nothing to declare", "no", "none", "n/a", "não", "nao"),
                    fallback="I have nothing to declare.",
                )
            if "if you checked any of the options above" in lower:
                return "N/A - Nothing to declare."
            company_name = (job.get("company") or "").strip()
            if "wellhub" in company_name.lower() or "wellhub" in lower:
                return (
                    "I am the owner/developer of Matchly TCG, a platform focused on trading card games (TCGs) "
                    "and local game stores. It does not operate in health, wellness, fitness, or corporate benefits, "
                    "nor does it compete with or have any business relationship with Wellhub."
                )
            target_co = company_name or "the company"
            return (
                "I am the owner/developer of Matchly TCG, a platform focused on trading card games (TCGs) "
                f"and local game stores. It does not compete with or have any business relationship with {target_co}."
            )

        # Portal alert / notification frequency (non-factual UI preference)
        if any(k in lower for k in ("frequência", "frequencia", "frequency")) and any(
            k in lower for k in ("alerta", "alert", "notifica", "notification", "dias", "days")
        ):
            if field.options:
                return self._pick_best_option(
                    field.options,
                    preferred=("7", "semanal", "weekly", "15", "quinzenal", "30", "mensal", "monthly", "1", "diar", "daily"),
                    fallback=field.options[0],
                )
            return "7"

        # Education fields (verified IFPE Associate Degree in Systems Analysis and Development, started 2022)
        clean_lbl = re.sub(r"[\s*✱:]+$", "", field.label.strip().lower())
        if (
            field.name in ("school--0", "school")
            or "school" in field.name.lower()
            or lower.startswith("school ")
            or "school name" in lower
            or "nome da instituição" in lower
            or "nome da instituicao" in lower
            or clean_lbl in ("school", "instituição", "instituicao", "instituição de ensino", "instituicao de ensino", "university", "college", "faculdade", "escola")
        ):
            school_name = p.education_school or "Federal Institute of Education, Science and Technology of Pernambuco (IFPE) – Jaboatão Campus"
            if field.options or field_type in ("select", "combobox") or field.name.startswith("school--"):
                return self._pick_best_option(
                    field.options,
                    preferred=("ifpe", "instituto federal", "federal institute", "other", "outra"),
                    fallback=school_name,
                )
            return p.education_summary or f"Associate Degree in Systems Analysis and Development | {school_name} | 02/2022 – In progress"
        if field.name in ("degree--0", "degree") or lower.startswith("degree ") or clean_lbl in ("degree", "grau", "formação", "formacao"):
            return self._pick_best_option(
                field.options,
                preferred=("associate", "tecnólogo", "tecnologo", "bachelor", "bacharel"),
                fallback="Associate's Degree",
            )
        if field.name in ("discipline--0", "discipline") or lower.startswith("discipline ") or clean_lbl in ("discipline", "field of study", "curso", "área de estudo"):
            return self._pick_best_option(
                field.options,
                preferred=("computer science", "information systems", "systems analysis", "software", "engineering", "ciência da computação", "sistemas"),
                fallback="Systems Analysis and Development",
            )
        if field.name in ("start-year--0", "start_year") or "start date year" in lower:
            return "2022"
        if field.name in ("end-year--0", "end_year") or "end date year" in lower:
            return "2026"

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
            profile_facts=smart_truncate(self._profile.as_verified_facts_text(), 5000),
            resume_excerpt=smart_truncate(resume_excerpt, 3500),
            company=job.get("company") or "—",
            title=job.get("title") or "—",
            job_description=(job.get("body") or "(não informada)")[:1500],
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
        self._allow_ai = allow_ai_qa
        self._answerer = VerifiedFactAnswerer(
            profile, allow_ai=allow_ai_qa, allow_salary_web=allow_salary_web
        )

    @staticmethod
    def _click_resilient(page: Any, loc: Any, *, timeout: int = 5000, scroll: bool = True) -> None:
        """Click a locator, dismissing Shadow DOM / cookie consent overlays if pointer events are intercepted."""
        if scroll and hasattr(loc, "scroll_into_view_if_needed"):
            try:
                loc.scroll_into_view_if_needed(timeout=min(timeout, 2000))
            except Exception:  # noqa: BLE001
                pass
        try:
            loc.click(timeout=timeout)
            return
        except Exception as exc:  # noqa: BLE001
            err_low = str(exc).lower()
            is_intercepted = (
                "intercepts pointer events" in err_low
                or "intercepted" in err_low
                or "obscured" in err_low
                or "timeout" in err_low
            )
            if is_intercepted and dismiss_blocking_overlays(page, aggressive=True):
                if scroll and hasattr(loc, "scroll_into_view_if_needed"):
                    try:
                        loc.scroll_into_view_if_needed(timeout=min(timeout, 2000))
                    except Exception:  # noqa: BLE001
                        pass
                loc.click(timeout=timeout)
                return
            raise

    @staticmethod
    def _snapshot_pages(page: Any) -> list[Any]:
        ctx = getattr(page, "context", None)
        if ctx is not None and hasattr(ctx, "pages"):
            try:
                return list(ctx.pages)
            except Exception:  # noqa: BLE001
                pass
        return []

    @staticmethod
    def _capture_new_tab(page: Any, before_pages: list[Any], *, wait_ms: int = 800) -> Any | None:
        """Return a newly opened non-blank tab spawned by clicking a CTA/button, or None."""
        ctx = getattr(page, "context", None)
        if ctx is None or not hasattr(ctx, "pages"):
            return None
        new_pages: list[Any] = []
        attempts = max(1, wait_ms // 150)
        for idx in range(attempts):
            try:
                current_pages = list(ctx.pages)
            except Exception:  # noqa: BLE001
                return None
            new_pages = [p for p in current_pages if p not in before_pages and p is not page]
            if new_pages:
                break
            if idx + 1 < attempts and hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(150)
        if not new_pages:
            return None
        new_page = new_pages[-1]
        try:
            if hasattr(new_page, "wait_for_load_state"):
                new_page.wait_for_load_state("domcontentloaded", timeout=8000)
            for _ in range(15):
                cur = str(getattr(new_page, "url", "") or "").strip()
                if cur and cur != "about:blank":
                    break
                if hasattr(new_page, "wait_for_timeout"):
                    new_page.wait_for_timeout(200)
            if hasattr(new_page, "wait_for_load_state"):
                new_page.wait_for_load_state("domcontentloaded", timeout=8000)
            if hasattr(new_page, "wait_for_timeout"):
                new_page.wait_for_timeout(600)
        except Exception:  # noqa: BLE001
            pass
        new_url = str(getattr(new_page, "url", "") or "").strip()
        if not new_url or new_url == "about:blank" or new_url == getattr(page, "url", ""):
            return None
        return new_page

    def _delegate_to_new_tab(
        self,
        new_page: Any,
        *,
        platform: str,
        fallback_url: str,
        job: dict,
        documents: ApplicationDocuments,
        dry_run: bool,
        timeout_seconds: int,
        cover_letter_provider: CoverLetterCallback | None,
        max_steps: int,
        popup_depth: int,
    ) -> ApplicationOutcome:
        target_url = str(getattr(new_page, "url", "") or fallback_url)
        logger.info("Following application form opened in new tab: %s", target_url)
        try:
            return self.run_on_page(
                new_page,
                platform=platform,
                apply_url=target_url,
                job=job,
                documents=documents,
                dry_run=dry_run,
                timeout_seconds=timeout_seconds,
                cover_letter_provider=cover_letter_provider,
                scope_selector=None,
                max_steps=max_steps,
                _popup_depth=popup_depth + 1,
            )
        finally:
            try:
                if hasattr(new_page, "close"):
                    new_page.close()
            except Exception:  # noqa: BLE001
                pass

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
        _popup_depth: int = 0,
    ) -> ApplicationOutcome:
        """Inspect page, guard against blockers, fill verified fields (multi-step aware), validate, and submit."""
        current_url = page.url or apply_url
        body_text = page.inner_text("body") if page.locator("body").count() else ""

        # 1. Hard compliance/security checks before touching any fields
        for check in (
            detect_cloudflare_block(page, response=getattr(page, "_jobbot_last_response", None), body_text=body_text),
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
                    reason=(
                        "Portal agregador (Jobicy) exige login/cadastro próprio para revelar o link externo da vaga. "
                        "Execute `.venv/bin/python -m bot.login jobicy` para autenticar via Google."
                    ),
                    dry_run=dry_run,
                )
        except Exception:  # noqa: BLE001
            pass

        resume_excerpt = ""
        if documents.resume_md and Path(documents.resume_md).exists():
            resume_excerpt = smart_truncate(Path(documents.resume_md).read_text(encoding="utf-8"), 4000)

        filled_fields: dict[str, str] = {}
        cover_letter_requested = False
        cover_pdf = documents.cover_letter_pdf
        cover_text = documents.cover_letter_text
        any_field_found = False
        resume_uploaded_via_btn = False
        form_target: Any = page
        tried_cta = False
        ai_dom_budget = [3]
        resolved_submit_btn: Any = None
        ai_diagnosis = ""

        # Dismiss common cookie/consent overlays (including Shadow DOM banners like Transcend & Didomi)
        # proactively so they cannot intercept clicks on fields or navigation/submit buttons.
        dismiss_blocking_overlays(page)

        for step in range(1, max_steps + 1):
            current_url = page.url or apply_url
            target_scope = scope_selector if form_target is page else None
            form_target, raw_fields = self._find_form_target(page, preferred=form_target, scope_selector=scope_selector)
            target_scope = scope_selector if form_target is page else None
            if not raw_fields and not any_field_found and hasattr(page, "wait_for_timeout"):
                for _ in range(8):
                    page.wait_for_timeout(500)
                    form_target, raw_fields = self._find_form_target(page, preferred=form_target, scope_selector=scope_selector)
                    if raw_fields:
                        break
                if not raw_fields and not scope_selector and not tried_cta:
                    tried_cta = True
                    before_pages = self._snapshot_pages(page)
                    if self._try_open_initial_apply_cta(page):
                        if _popup_depth < 2:
                            new_tab = self._capture_new_tab(page, before_pages)
                            if new_tab is not None:
                                return self._delegate_to_new_tab(
                                    new_tab,
                                    platform=platform,
                                    fallback_url=current_url,
                                    job=job,
                                    documents=documents,
                                    dry_run=dry_run,
                                    timeout_seconds=timeout_seconds,
                                    cover_letter_provider=cover_letter_provider,
                                    max_steps=max_steps,
                                    popup_depth=_popup_depth,
                                )
                        for _ in range(8):
                            page.wait_for_timeout(500)
                            form_target, raw_fields = self._find_form_target(page, scope_selector=scope_selector)
                            if raw_fields:
                                break
                target_scope = scope_selector if form_target is page else None
            if not raw_fields and not any_field_found:
                cf_or_cap = detect_cloudflare_block(page) or detect_active_captcha_on_page(page)
                if cf_or_cap:
                    return ApplicationOutcome(
                        status="pending_human_action",
                        platform=platform,
                        apply_url=page.url or current_url,
                        reason=cf_or_cap,
                        dry_run=dry_run,
                    )
                try:
                    page_title = page.title() if hasattr(page, "title") else ""
                    head_body = page.inner_text("body")[:3000] if page.locator("body").count() else ""
                    if _FORM_EXPIRED_RE.search(f"{page_title}\n{head_body}"):
                        return ApplicationOutcome(
                            status="expired",
                            platform=platform,
                            apply_url=page.url or current_url,
                            reason="Vaga encerrada ou indisponível na plataforma.",
                            dry_run=dry_run,
                        )
                except Exception:  # noqa: BLE001
                    pass

                # Consult AI to inspect page HTML/DOM before escalating missing form/CTA
                ai_form = ask_ai_dom_recovery(
                    page,
                    goal="locate_form_or_apply_cta",
                    error_context="Formulário de candidatura não encontrado pelos seletores automáticos.",
                    job=job,
                    scope_selector=target_scope,
                    allow_ai=self._allow_ai,
                    max_calls_budget=ai_dom_budget,
                )
                if ai_form:
                    ai_diagnosis = (ai_form.get("diagnosis") or "").strip()
                    ai_status = ai_form.get("page_status") or ""
                    if ai_status == "expired":
                        return ApplicationOutcome(
                            status="expired",
                            platform=platform,
                            apply_url=page.url or current_url,
                            reason=ai_diagnosis or "Vaga encerrada ou indisponível na plataforma.",
                            dry_run=dry_run,
                        )
                    if ai_status in ("auth_wall", "captcha_or_block"):
                        return ApplicationOutcome(
                            status="pending_human_action",
                            platform=platform,
                            apply_url=page.url or current_url,
                            reason=ai_diagnosis or "Login ou verificação anti-bot detectada na página.",
                            dry_run=dry_run,
                        )
                    if ai_form.get("dismiss_overlay_selector"):
                        try_dismiss_overlay(page, ai_form["dismiss_overlay_selector"], wait_page=page)
                    if ai_form.get("apply_cta_selector"):
                        cta_loc = resolve_locator(page, ai_form["apply_cta_selector"], scope_selector=target_scope)
                        if cta_loc is not None:
                            before_pages = self._snapshot_pages(page)
                            try:
                                self._click_resilient(page, cta_loc, timeout=5000)
                                if _popup_depth < 2:
                                    new_tab = self._capture_new_tab(page, before_pages)
                                    if new_tab is not None:
                                        return self._delegate_to_new_tab(
                                            new_tab,
                                            platform=platform,
                                            fallback_url=current_url,
                                            job=job,
                                            documents=documents,
                                            dry_run=dry_run,
                                            timeout_seconds=timeout_seconds,
                                            cover_letter_provider=cover_letter_provider,
                                            max_steps=max_steps,
                                            popup_depth=_popup_depth,
                                        )
                                for _ in range(8):
                                    if hasattr(page, "wait_for_timeout"):
                                        page.wait_for_timeout(450)
                                    form_target, raw_fields = self._find_form_target(page, scope_selector=scope_selector)
                                    if raw_fields:
                                        break
                            except Exception as exc:  # noqa: BLE001
                                logger.debug("AI apply CTA click failed: %s", exc)
                    if not raw_fields and ai_form.get("iframe_selector"):
                        fr = resolve_iframe_target(page, ai_form["iframe_selector"])
                        if fr is not None:
                            fr_fields = self._extract_fields(fr, scope_selector=None)
                            if fr_fields:
                                form_target, raw_fields = fr, fr_fields
                    if not raw_fields and ai_form.get("fields"):
                        raw_fields = validate_ai_fields(form_target, ai_form.get("fields"))
                    target_scope = scope_selector if form_target is page else None

            if not raw_fields and not any_field_found:
                diag_note = f" (Diagnóstico IA: {ai_diagnosis})" if ai_diagnosis else ""
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=platform,
                    apply_url=page.url or current_url,
                    reason=f"Formulário de candidatura não encontrado ou carregado dinamicamente fora do padrão.{diag_note}",
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
                if self._try_upload_via_button(page, target_scope, documents.resume_pdf):
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
                        upload_path = cover_pdf if is_cover_letter_label(lower) else documents.resume_pdf
                        if upload_path and Path(upload_path).exists():
                            try:
                                fld_loc = form_target.locator(fld.selector).first
                                is_airtable_attach = False
                                if hasattr(fld_loc, "get_attribute"):
                                    try:
                                        is_airtable_attach = bool(fld_loc.get_attribute("data-jobbot-airtable-attach", timeout=300))
                                    except Exception:  # noqa: BLE001
                                        is_airtable_attach = False
                                if is_airtable_attach:
                                    self._upload_airtable_attachment(page, fld_loc, upload_path)
                                else:
                                    fld_loc.set_input_files(upload_path)
                            except Exception as exc:  # noqa: BLE001
                                ai_file = ask_ai_dom_recovery(
                                    form_target,
                                    goal="locate_or_fix_field",
                                    error_context=f"Erro HTML ao anexar arquivo em '{key_label}' ({fld.selector}): {exc}",
                                    job=job,
                                    scope_selector=target_scope,
                                    field=fld,
                                    allow_ai=self._allow_ai,
                                    max_calls_budget=ai_dom_budget,
                                )
                                alt_sel = (ai_file or {}).get("target_field_selector") or ""
                                alt_loc = resolve_locator(form_target, alt_sel, scope_selector=target_scope, require_visible=False)
                                if alt_loc is None:
                                    raise HumanInterventionRequired(
                                        f"Erro HTML ao anexar arquivo em '{key_label}': {exc}",
                                        category="html_error",
                                    ) from exc
                                alt_loc.set_input_files(upload_path)
                            filled_fields[key_label] = f"[FILE] {Path(upload_path).name}"
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

                    try:
                        self._fill_control(form_target, fld, answer, wait_page=page)
                    except HumanInterventionRequired:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        ai_fld = ask_ai_dom_recovery(
                            form_target,
                            goal="locate_or_fix_field",
                            error_context=f"Erro HTML ao preencher '{key_label}' ({fld.field_type}, {fld.selector}): {exc}",
                            job=job,
                            scope_selector=target_scope,
                            field=fld,
                            allow_ai=self._allow_ai,
                            max_calls_budget=ai_dom_budget,
                        )
                        if ai_fld and ai_fld.get("dismiss_overlay_selector"):
                            try_dismiss_overlay(form_target, ai_fld["dismiss_overlay_selector"], wait_page=page)
                        alt_sel = (ai_fld or {}).get("target_field_selector") or ""
                        alt_loc = resolve_locator(form_target, alt_sel, scope_selector=target_scope)
                        if alt_loc is None:
                            raise HumanInterventionRequired(
                                f"Erro HTML ao preencher campo '{key_label}': {exc}",
                                category="html_error",
                            ) from exc
                        alt_type = ((ai_fld or {}).get("target_field_type") or fld.field_type).strip().lower()
                        patched_fld = FormField(
                            name=fld.name,
                            label=fld.label,
                            field_type=alt_type,
                            required=fld.required,
                            selector=alt_sel,
                            options=fld.options,
                            placeholder=fld.placeholder,
                        )
                        try:
                            self._fill_control(form_target, patched_fld, answer, wait_page=page)
                        except HumanInterventionRequired:
                            raise
                        except Exception as retry_exc:  # noqa: BLE001
                            raise HumanInterventionRequired(
                                f"Erro HTML ao preencher campo '{key_label}': {retry_exc}",
                                category="html_error",
                            ) from retry_exc
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
            resolved_submit_btn = self._find_submit_button(form_target, scope_selector=target_scope)
            has_submit = resolved_submit_btn is not None
            if not has_submit:
                next_btn = self._find_next_button(form_target, scope_selector=target_scope) if step < max_steps else None
                if next_btn is None:
                    ai_nav = ask_ai_dom_recovery(
                        form_target,
                        goal="locate_submit_or_next_button",
                        error_context="Botão de envio (Submit) ou avanço (Next/Avançar) não identificado pelos seletores padrão.",
                        job=job,
                        scope_selector=target_scope,
                        allow_ai=self._allow_ai,
                        max_calls_budget=ai_dom_budget,
                    )
                    if ai_nav:
                        if ai_nav.get("dismiss_overlay_selector"):
                            try_dismiss_overlay(form_target, ai_nav["dismiss_overlay_selector"], wait_page=page)
                        if ai_nav.get("page_status") in ("auth_wall", "captcha_or_block"):
                            return ApplicationOutcome(
                                status="pending_human_action",
                                platform=platform,
                                apply_url=page.url or current_url,
                                reason=ai_nav.get("diagnosis") or "Login ou verificação anti-bot detectada pela IA.",
                                cover_letter_requested=cover_letter_requested,
                                filled_fields=filled_fields,
                                dry_run=dry_run,
                            )
                        if step < max_steps and ai_nav.get("next_button_selector"):
                            next_btn = resolve_locator(form_target, ai_nav["next_button_selector"], scope_selector=target_scope)
                        if next_btn is None and ai_nav.get("submit_button_selector"):
                            resolved_submit_btn = resolve_locator(
                                form_target, ai_nav["submit_button_selector"], scope_selector=target_scope
                            )
                if next_btn is not None and step < max_steps:
                    before_sig = self._step_signature(form_target, scope_selector=target_scope)
                    try:
                        self._click_resilient(page, next_btn, timeout=5000)
                        page.wait_for_timeout(900)
                    except Exception as exc:  # noqa: BLE001
                        recovered_click = False
                        ai_err = ask_ai_dom_recovery(
                            form_target,
                            goal="resolve_click_or_html_error",
                            error_context=f"Erro ao clicar no botão de avanço da etapa {step}: {exc}",
                            job=job,
                            scope_selector=target_scope,
                            allow_ai=self._allow_ai,
                            max_calls_budget=ai_dom_budget,
                        )
                        if ai_err:
                            if ai_err.get("dismiss_overlay_selector"):
                                try_dismiss_overlay(form_target, ai_err["dismiss_overlay_selector"], wait_page=page)
                            alt_next = resolve_locator(
                                form_target,
                                ai_err.get("next_button_selector") or "",
                                scope_selector=target_scope,
                            ) or next_btn
                            try:
                                self._click_resilient(page, alt_next, timeout=5000)
                                page.wait_for_timeout(900)
                                recovered_click = True
                            except Exception:  # noqa: BLE001
                                pass
                        if not recovered_click:
                            return ApplicationOutcome(
                                status="pending_human_action",
                                platform=platform,
                                apply_url=current_url,
                                reason=f"Falha ao avançar etapa {step} do formulário: {exc}",
                                cover_letter_requested=cover_letter_requested,
                                filled_fields=filled_fields,
                                dry_run=dry_run,
                            )
                    cap_step = detect_cloudflare_block(page) or detect_active_captcha_on_page(page)
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
                    after_sig = self._step_signature(form_target, scope_selector=target_scope)
                    if before_sig and after_sig == before_sig:
                        page.wait_for_timeout(900)
                        after_sig = self._step_signature(form_target, scope_selector=target_scope)
                    if before_sig and after_sig == before_sig:
                        val_err = self._visible_validation_error(form_target, scope_selector=target_scope)
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
        submit_btn = resolved_submit_btn or self._find_submit_button(form_target, scope_selector=target_scope)
        if submit_btn is None:
            ai_sub = ask_ai_dom_recovery(
                form_target,
                goal="locate_submit_button",
                error_context="Botão final de envio (Submit/Enviar) não identificado pelos seletores padrão.",
                job=job,
                scope_selector=target_scope,
                allow_ai=self._allow_ai,
                max_calls_budget=ai_dom_budget,
            )
            if ai_sub:
                if ai_sub.get("dismiss_overlay_selector"):
                    try_dismiss_overlay(form_target, ai_sub["dismiss_overlay_selector"], wait_page=page)
                submit_btn = resolve_locator(
                    form_target,
                    ai_sub.get("submit_button_selector") or "",
                    scope_selector=target_scope,
                )
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

        dismiss_blocking_overlays(page)
        before_submit_pages = self._snapshot_pages(page)
        try:
            self._click_resilient(page, submit_btn, timeout=8000)
        except Exception as exc:  # noqa: BLE001
            ai_click = ask_ai_dom_recovery(
                form_target,
                goal="resolve_click_or_html_error",
                error_context=f"Erro ao clicar no botão de envio: {exc}",
                job=job,
                scope_selector=target_scope,
                allow_ai=self._allow_ai,
                max_calls_budget=ai_dom_budget,
            )
            clicked_after_ai = False
            if ai_click:
                if ai_click.get("dismiss_overlay_selector"):
                    try_dismiss_overlay(form_target, ai_click["dismiss_overlay_selector"], wait_page=page)
                alt_submit = resolve_locator(
                    form_target,
                    ai_click.get("submit_button_selector") or "",
                    scope_selector=target_scope,
                ) or submit_btn
                try:
                    self._click_resilient(page, alt_submit, timeout=8000)
                    clicked_after_ai = True
                except Exception:  # noqa: BLE001
                    pass
            if not clicked_after_ai:
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

        # If clicking the button opened a new tab/popup with the actual form or confirmation, follow it
        if _popup_depth < 1:
            new_tab = self._capture_new_tab(page, before_submit_pages)
            if new_tab is not None:
                try:
                    tab_url = str(getattr(new_tab, "url", "") or current_url)
                    tab_text = new_tab.inner_text("body") if new_tab.locator("body").count() else ""
                    tab_conf, tab_cid = detect_submission_confirmation(tab_url, tab_text)
                    if tab_conf:
                        return ApplicationOutcome(
                            status="applied",
                            platform=platform,
                            apply_url=tab_url,
                            confirmation_id=tab_cid,
                            cover_letter_requested=cover_letter_requested,
                            filled_fields=filled_fields,
                            dry_run=False,
                        )
                    _, tab_fields = self._find_form_target(new_tab)
                    if tab_fields:
                        return self._delegate_to_new_tab(
                            new_tab,
                            platform=platform,
                            fallback_url=tab_url,
                            job=job,
                            documents=documents,
                            dry_run=dry_run,
                            timeout_seconds=timeout_seconds,
                            cover_letter_provider=cover_letter_provider,
                            max_steps=max_steps,
                            popup_depth=_popup_depth,
                        )
                except Exception:  # noqa: BLE001
                    pass

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
                if form_target is not page and form_target.locator("body").count():
                    post_text = f"{post_text}\n{form_target.inner_text('body')}"
            except Exception:  # noqa: BLE001
                continue
            confirmed, conf_id = detect_submission_confirmation(post_url, post_text)
            if confirmed:
                break
            if self._visible_validation_error(form_target, scope_selector=target_scope):
                break

        captcha_after = detect_cloudflare_block(page) or detect_active_captcha_on_page(page)
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
        val_err = self._visible_validation_error(form_target, scope_selector=target_scope)
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

    @classmethod
    def _find_form_target(
        cls,
        page: Any,
        *,
        preferred: Any = None,
        scope_selector: str | None = None,
    ) -> tuple[Any, list[FormField]]:
        """Extract fields from the preferred target, main page, or embedded non-noise iframes."""
        if preferred is not None and preferred is not page:
            fields = cls._extract_fields(preferred, scope_selector=None)
            if fields:
                return preferred, fields
        fields = cls._extract_fields(page, scope_selector=scope_selector)
        if fields:
            return page, fields
        if not scope_selector and hasattr(page, "frames"):
            try:
                main_frame = getattr(page, "main_frame", None)
                for fr in list(page.frames):
                    if fr is main_frame:
                        continue
                    fr_url = (getattr(fr, "url", "") or "").lower()
                    if fr_url in ("", "about:blank") or _NOISE_FRAME_RE.search(fr_url):
                        continue
                    fr_fields = cls._extract_fields(fr, scope_selector=None)
                    if fr_fields:
                        return fr, fr_fields
            except Exception:  # noqa: BLE001
                pass
        return page, []

    @staticmethod
    def _try_open_initial_apply_cta(page: Any) -> bool:
        """Click an initial visible 'Apply / Candidatar-se' CTA on landing pages that hide the form behind a button."""
        if not hasattr(page, "evaluate"):
            return False
        try:
            marked = page.evaluate(
                r"""() => {
                    document.querySelectorAll('[data-jobbot-init-cta]').forEach(e => e.removeAttribute('data-jobbot-init-cta'));
                    const isVis = (el) => {
                        if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
                        const s = getComputedStyle(el);
                        const r = el.getBoundingClientRect();
                        return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0 && r.width > 20 && r.height > 15;
                    };
                    const els = Array.from(document.querySelectorAll('main button, main a, article button, article a, button, a'));
                    for (const el of els) {
                        if (!isVis(el) || el.closest('header, nav, footer')) continue;
                        const href = (el.getAttribute('href') || '').toLowerCase();
                        if (/(login|signin|sign_in|signup|sign_up|register|cadastro)/.test(href)) continue;
                        const txt = (el.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase();
                        if (!txt || txt.length > 45) continue;
                        if (/^(apply|apply now|apply for this( (job|position|role))?|start application|candidatar-?se|quero me candidatar|inscrever-?se|enviar curr[ií]culo)$/.test(txt)) {
                            el.setAttribute('data-jobbot-init-cta', '1');
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if not marked:
                return False
            btn = page.locator("[data-jobbot-init-cta='1']").first
            PlaywrightFormFiller._click_resilient(page, btn, timeout=4000)
            return True
        except Exception:  # noqa: BLE001
            return False

    @classmethod
    def _upload_airtable_attachment(cls, page: Any, attach_btn: Any, upload_path: str) -> None:
        """Upload a file through Airtable's Uppy attachment modal."""
        cls._click_resilient(page, attach_btn, timeout=4000)
        if hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(450)
        file_input = page.locator("input.uppy-Dashboard-input[type='file'], [role='dialog'] input[type='file']").first
        if hasattr(file_input, "wait_for"):
            file_input.wait_for(state="attached", timeout=4000)
        file_input.set_input_files(upload_path)
        if hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(600)
        upload_btn = page.locator(".uppy-StatusBar-actionBtn--upload, button.uppy-StatusBar-actionBtn").first
        if upload_btn.count() > 0:
            try:
                if upload_btn.is_visible(timeout=2000):
                    cls._click_resilient(page, upload_btn, timeout=4000)
            except Exception:  # noqa: BLE001
                pass
        # Wait for Uppy modal to finish uploading and close
        if hasattr(page, "wait_for_timeout"):
            for _ in range(20):
                page.wait_for_timeout(500)
                try:
                    if page.locator("input.uppy-Dashboard-input[type='file']").count() == 0:
                        break
                    dlg = page.locator("[role='dialog']:has(.uppy-Dashboard)").first
                    if dlg.count() == 0 or not dlg.is_visible(timeout=100):
                        break
                except Exception:  # noqa: BLE001
                    break

    @staticmethod
    def _extract_fields(page: Any, *, scope_selector: str | None = None) -> list[FormField]:
        js = r"""(scopeSel) => {
            const IGNORE_NOISE = /(newsletter|cookie|consent|analytics|ads|search|busca|pesquisa|password|senha|login|signin|beecatcher|robots only|g-recaptcha-response|h-captcha-response|cf-turnstile-response|global_resume_upload|resume_upload_inner_block|banner-special)/i;
            document.querySelectorAll('[data-jobbot-field]').forEach(e => e.removeAttribute('data-jobbot-field'));
            document.querySelectorAll('[data-jobbot-airtable-attach], [data-jobbot-airtable-fk]').forEach(e => {
                e.removeAttribute('data-jobbot-airtable-attach');
                e.removeAttribute('data-jobbot-airtable-fk');
            });
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
            const QUERY_SEL = [
                'input',
                'textarea',
                'select',
                '.ashby-application-form-input-yesno',
                '[role="radiogroup"]',
                '[role="combobox"]',
                '[role="textbox"][contenteditable]',
                '[data-columntype="multipleAttachment"] button',
                '[data-columntype="foreignKey"] button',
            ].join(', ');
            // Prefer inputs inside a non-newsletter/non-search <form>, fallback to all inputs in root (Ashby/React/Airtable/LinkedIn SDUI)
            let candidates = [];
            const forms = Array.from(root.querySelectorAll('form')).filter(f => {
                const meta = ((f.id || '') + ' ' + (f.className || '') + ' ' + (f.action || '')).toLowerCase();
                if (/(newsletter|search|login|signin|subscribe)/i.test(meta)) return false;
                const nonHidden = f.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select');
                return nonHidden.length >= 2 || f.querySelector('input[type="file"]');
            });
            if (forms.length > 0) {
                for (const f of forms) {
                    candidates.push(...Array.from(f.querySelectorAll(QUERY_SEL)));
                }
            } else {
                candidates = Array.from(root.querySelectorAll(QUERY_SEL));
            }

            const out = [];
            let autoIdx = 0;
            const handledRadioGroups = new Set();
            const seenEls = new Set();

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
                if (seenEls.has(el)) continue;
                seenEls.add(el);
                if (el.classList && el.classList.contains('ashby-application-form-input-yesno')) {
                    const entry = el.closest('.ashby-application-form-field-entry');
                    const lblEl = entry ? entry.querySelector('label') : null;
                    const qLabel = lblEl ? lblEl.innerText.trim() : '';
                    const btns = Array.from(el.querySelectorAll('button.ashby-application-form-input-yesno-option'));
                    if (qLabel && btns.length >= 2) {
                        autoIdx += 1;
                        const marker = `jb-fld-${autoIdx}`;
                        const opts = [];
                        for (const b of btns) {
                            const oTxt = (b.innerText || '').trim();
                            if (!oTxt) continue;
                            b.setAttribute('data-jobbot-radio-group', marker);
                            b.setAttribute('data-jobbot-radio-opt', oTxt);
                            opts.push(oTxt);
                        }
                        if (opts.length > 0) {
                            el.setAttribute('data-jobbot-field', marker);
                            const req = Boolean(lblEl && (lblEl.className || '').includes('required'));
                            out.push({
                                name: entry?.getAttribute('data-field-path') || '',
                                label: qLabel.replace(/\s+/g, ' ').trim(),
                                field_type: 'radio',
                                required: req,
                                selector: `[data-jobbot-field="${marker}"]`,
                                options: opts,
                                placeholder: '',
                            });
                        }
                    }
                    continue;
                }

                const tag = el.tagName.toLowerCase();
                const elRole = (el.getAttribute('role') || '').toLowerCase();
                const isAirtableAttachBtn = tag === 'button' && Boolean(el.closest('[data-columntype="multipleAttachment"]'));
                const isAirtableFkBtn = tag === 'button' && Boolean(el.closest('[data-columntype="foreignKey"]'));

                // ARIA radiogroup (e.g. Airtable <ul role="radiogroup"> with <li role="radio">)
                if (elRole === 'radiogroup') {
                    // If this radiogroup contains native <input type="radio">, let the native radio handler group them
                    if (el.querySelector('input[type="radio"]')) continue;
                    const cs = window.getComputedStyle(el);
                    const cr = el.getBoundingClientRect();
                    if (cs.display === 'none' || cs.visibility === 'hidden' || cr.width <= 1 || cr.height <= 1) continue;
                    const items = Array.from(el.querySelectorAll('[role="radio"]'));
                    if (items.length === 0) continue;
                    let qLabel = (el.getAttribute('aria-label') || '').trim();
                    if (!qLabel) {
                        const ariaBy = el.getAttribute('aria-labelledby') || '';
                        if (ariaBy) {
                            qLabel = ariaBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
                        }
                    }
                    if (!qLabel) {
                        const sf = el.closest('.sharedFormField');
                        const tl = sf ? sf.querySelector('.title') : null;
                        if (tl) qLabel = tl.innerText.trim();
                    }
                    if (!qLabel) continue;
                    autoIdx += 1;
                    const marker = `jb-fld-${autoIdx}`;
                    const opts = [];
                    for (const item of items) {
                        const oTxt = (item.innerText || item.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
                        if (!oTxt) continue;
                        item.setAttribute('data-jobbot-radio-group', marker);
                        item.setAttribute('data-jobbot-radio-opt', oTxt);
                        opts.push(oTxt);
                    }
                    if (opts.length === 0) continue;
                    el.setAttribute('data-jobbot-field', marker);
                    const req = Boolean(
                        el.getAttribute('aria-required') === 'true' ||
                        qLabel.includes('*') ||
                        el.closest('.sharedFormField.required')
                    );
                    out.push({
                        name: el.getAttribute('id') || '',
                        label: qLabel.replace(/\s+/g, ' ').trim(),
                        field_type: 'radio',
                        required: req,
                        selector: `[data-jobbot-field="${marker}"]`,
                        options: opts,
                        placeholder: '',
                    });
                    continue;
                }

                // Airtable attachment ("Attach file") & linked-record ("Add") buttons
                if (isAirtableAttachBtn || isAirtableFkBtn) {
                    const cs = window.getComputedStyle(el);
                    const cr = el.getBoundingClientRect();
                    if (cs.display === 'none' || cs.visibility === 'hidden' || cr.width <= 1 || cr.height <= 1) continue;
                    const sf = el.closest('.sharedFormField');
                    const tl = sf ? sf.querySelector('.title') : null;
                    const qLabel = tl ? tl.innerText.replace(/\s+/g, ' ').trim() : (el.getAttribute('aria-label') || el.innerText || '').trim();
                    if (!qLabel) continue;
                    if (isAirtableAttachBtn) {
                        const cell = el.closest('[data-columntype="multipleAttachment"]');
                        if (cell && cell.querySelector('.attachmentThumbnail, [data-testid*="attachment" i], button[aria-label*="Remove" i]')) {
                            continue;
                        }
                    }
                    autoIdx += 1;
                    const marker = `jb-fld-${autoIdx}`;
                    el.setAttribute('data-jobbot-field', marker);
                    if (isAirtableAttachBtn) el.setAttribute('data-jobbot-airtable-attach', '1');
                    if (isAirtableFkBtn) el.setAttribute('data-jobbot-airtable-fk', '1');
                    const req = Boolean(qLabel.includes('*') || (sf && sf.classList.contains('required')));
                    out.push({
                        name: '',
                        label: qLabel,
                        field_type: isAirtableAttachBtn ? 'file' : 'combobox',
                        required: req,
                        selector: `[data-jobbot-field="${marker}"]`,
                        options: [],
                        placeholder: '',
                    });
                    continue;
                }

                // Skip temporary search comboboxes inside open picker dialogs
                if (elRole === 'combobox' && el.closest('[role="dialog"]')) continue;

                const type = (el.getAttribute('type') || tag).toLowerCase();
                if (tag !== 'div' && tag !== 'span' && ['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
                if (el.disabled) continue;
                // Skip Greenhouse's hidden react-select required validation input & Ashby's hidden yes/no backing checkbox
                const clsStr = (el.className || '').toString();
                if (clsStr.includes('requiredInput') || el.closest('.ashby-application-form-input-yesno')) continue;
                if (el.closest('.select-shell') && el.getAttribute('role') !== 'combobox') continue;
                if (type === 'file' && el.closest('.ashby-application-form-autofill-input-root')) continue;

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
                    if (!qLabel) {
                        const leverQ = el.closest('.application-question');
                        if (leverQ) {
                            const txtEl = leverQ.querySelector('.text');
                            if (txtEl) qLabel = txtEl.innerText.trim();
                        }
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
                    // Check preceding sibling label or container label (Chakra/React/Ashby/Lever/Airtable)
                    const group = el.closest('.sharedFormField, [role="group"], .form-group, .field, .mb-3, .chakra-form-control, .ashby-application-form-field-entry, .application-question');
                    if (group) {
                        const gl = group.querySelector('.title, label, .text');
                        if (gl) label = gl.innerText.trim();
                    }
                }
                if (type === 'checkbox') {
                    const fs = el.closest('fieldset');
                    const leg = fs ? fs.querySelector('legend') : null;
                    if (leg && leg.innerText.trim()) {
                        const legTxt = leg.innerText.trim();
                        if (label && !label.includes(legTxt.slice(0, 25))) {
                            label = `${legTxt} — ${label}`;
                        } else if (!label) {
                            label = legTxt;
                        }
                    }
                }
                if (!label) {
                    const ariaBy = el.getAttribute('aria-labelledby') || '';
                    if (ariaBy) {
                        label = ariaBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
                    }
                }
                let placeholder = el.getAttribute('placeholder') || '';
                if (!placeholder) {
                    const sfDesc = el.closest('.sharedFormField')?.querySelector('.description');
                    if (sfDesc) placeholder = sfDesc.innerText.replace(/\s+/g, ' ').trim().slice(0, 120);
                }
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
                    (type !== 'checkbox' && el.required) ||
                    (type === 'checkbox' && el.required && el.name && !el.name.endsWith('[]')) ||
                    el.getAttribute('aria-required') === 'true' ||
                    label.includes('*') ||
                    label.includes('✱') ||
                    Boolean(el.closest('.sharedFormField.required')) ||
                    (el.closest('.ashby-application-form-field-entry')?.querySelector('label')?.className || '').includes('required')
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
                const isContentEditable = el.hasAttribute('contenteditable') && el.getAttribute('contenteditable') !== 'false';
                const resolvedType = (tag === 'textarea' || role === 'textbox' || isContentEditable)
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
    def _fill_control(page: Any, fld: FormField, answer: str, *, wait_page: Any = None) -> None:
        waiter = wait_page or page
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
            # Fallback for location selects where candidate city is absent -> prefer São Paulo before country-only overlap
            fld_meta = _norm(f"{fld.label} {fld.name}")
            is_loc_select = any(k in fld_meta for k in ("location", "localizacao", "city", "cidade"))
            if is_loc_select:
                for opt in fld.options:
                    nopt = _norm(opt)
                    if "sao paulo" in nopt:
                        loc.select_option(label=opt)
                        return

            # Token overlap match (e.g. "Recife, PE, Brazil" -> "Recife, Pernambuco, Brazil")
            ans_tokens = [t for t in re.split(r"[\s,/-]+", nans) if len(t) >= 3]
            if ans_tokens:
                best_opt = ""
                best_hits = 0
                for opt in fld.options:
                    nopt = _norm(opt)
                    if nopt in ("select...", "select", "selecione...", "selecione"):
                        continue
                    # Weight first token (city) higher than country
                    hits = sum(3 if idx == 0 else 1 for idx, tok in enumerate(ans_tokens) if tok in nopt)
                    if hits > best_hits:
                        best_hits = hits
                        best_opt = opt
                if best_opt and best_hits > 0:
                    loc.select_option(label=best_opt)
                    return
            # Fallback: try matching by index if option exists
            raise HumanInterventionRequired(
                f"Opção '{answer}' não encontrada no campo select '{fld.label or fld.name}' (opções: {', '.join(fld.options[:5])}).",
                category="select_mismatch",
            )
        elif fld.field_type == "combobox":
            PlaywrightFormFiller._click_resilient(waiter, loc, timeout=3000)
            waiter.wait_for_timeout(350)
            el_id = ""
            el_tag = ""
            try:
                el_id = (loc.get_attribute("id", timeout=300) or "").strip()
                if hasattr(loc, "evaluate"):
                    el_tag = str(loc.evaluate("e => e.tagName") or "").upper()
            except Exception:  # noqa: BLE001
                pass
            opt_selectors: list[str] = []
            if el_id:
                opt_selectors.append(f"[id^='react-select-{el_id}-option']")
            opt_selectors.extend(
                (
                    ".select__menu [role='option']",
                    "[id*='react-select'][role='option']",
                    "[role='dialog'] [role='option']",
                    "[role='listbox']:not(.iti__country-list) [role='option']:not([class*='iti__country'])",
                    "[class*='option' i][role='option']:not([class*='iti__country'])",
                    "[role='option']:not([class*='iti__country'])",
                )
            )

            def _option_matches(ntarget: str, otxt: str) -> bool:
                if not ntarget or not otxt:
                    return False
                if otxt == ntarget:
                    return True
                if len(ntarget) <= 3:
                    tokens = otxt.split()
                    return tokens[:1] == [ntarget] or ntarget in tokens
                return ntarget in otxt or otxt in ntarget

            def _try_click_option(target_text: str, *, allow_first_fallback: bool) -> bool:
                ntarget = _norm(target_text)
                candidates = [ntarget] if ntarget else []
                if ntarget in ("yes", "sim", "true", "acknowledge", "i agree", "concordo", "aceito"):
                    for syn in ("yes i agree", "yes", "sim", "acknowledge", "i agree", "agree", "concordo", "aceito", "understood", "i understand", "got it"):
                        if syn not in candidates:
                            candidates.append(syn)
                elif ntarget in ("no", "nao", "false"):
                    for syn in ("no", "nao", "none", "nenhuma"):
                        if syn not in candidates:
                            candidates.append(syn)

                for opt_sel in opt_selectors:
                    opts = page.locator(opt_sel)
                    cnt = min(opts.count(), 40)
                    if cnt == 0:
                        continue
                    visible_opts: list[tuple[Any, str]] = []
                    for i in range(cnt):
                        o = opts.nth(i)
                        try:
                            if not o.is_visible(timeout=200):
                                continue
                            otxt = _norm(o.inner_text(timeout=300) or "")
                            visible_opts.append((o, otxt))
                        except Exception:  # noqa: BLE001
                            continue
                    if not visible_opts:
                        continue
                    # 1. Exact match on candidate or synonym
                    for cand in candidates:
                        for o, otxt in visible_opts:
                            if otxt == cand:
                                try:
                                    o.click(timeout=2000)
                                    return True
                                except Exception:  # noqa: BLE001
                                    continue
                    # 2. Token/substring match on candidate or synonym
                    for cand in candidates:
                        for o, otxt in visible_opts:
                            if _option_matches(cand, otxt):
                                try:
                                    o.click(timeout=2000)
                                    return True
                                except Exception:  # noqa: BLE001
                                    continue
                    if allow_first_fallback:
                        try:
                            visible_opts[0][0].click(timeout=2000)
                            return True
                        except Exception:  # noqa: BLE001
                            pass
                return False

            def _type_combobox_search(text: str) -> None:
                if el_tag in ("INPUT", "TEXTAREA"):
                    loc.fill(text)
                    return
                # For Airtable foreignKey Add button or div[role="combobox"], check for a search input in the opened dialog/popover
                search_inp = page.locator(
                    "[role='dialog'] input[role='combobox'], [role='dialog'] input[placeholder*='Search' i], "
                    "[role='listbox'] input, [data-testid*='search' i] input"
                ).first
                try:
                    if search_inp.count() > 0 and search_inp.is_visible(timeout=250):
                        search_inp.fill(text)
                        return
                except Exception:  # noqa: BLE001
                    pass
                if hasattr(waiter, "keyboard"):
                    waiter.keyboard.type(text)

            chosen = _try_click_option(answer, allow_first_fallback=False)
            if not chosen:
                _type_combobox_search(answer)
                waiter.wait_for_timeout(650)
                chosen = _try_click_option(answer, allow_first_fallback=True)
            if not chosen and any(k in f"{el_id} {fld.name} {fld.label}".lower() for k in ("school", "institui", "universit", "college", "faculdade")):
                for school_try in ("Instituto Federal", "IFPE", "Other"):
                    _type_combobox_search(school_try)
                    waiter.wait_for_timeout(550)
                    chosen = _try_click_option(school_try, allow_first_fallback=True)
                    if chosen:
                        break
            if not chosen:
                try:
                    if el_tag in ("INPUT", "TEXTAREA"):
                        loc.press("Escape")
                    elif hasattr(waiter, "keyboard"):
                        waiter.keyboard.press("Escape")
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
                    tag_name = (target_radio.evaluate("e => e.tagName") or "").upper()
                    if tag_name != "INPUT":
                        PlaywrightFormFiller._click_resilient(waiter, target_radio, timeout=3000)
                    else:
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
            PlaywrightFormFiller._click_resilient(waiter, loc, timeout=3000)
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
                    inForm: Boolean(el.closest('form')),
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

        if kind == "submit":
            # Prefer buttons inside <form> and/or with type="submit" so header "Apply" scroll buttons outside <form> are not clicked
            ordered = sorted(
                buttons,
                key=lambda b: (
                    not bool(b.get("isSubmitAttr")),
                    not (bool(b.get("inForm")) and (b.get("type") == "submit")),
                    not bool(b.get("inForm")),
                    not (b.get("type") == "submit"),
                    b.get("idx", 0),
                ),
            )
        else:
            ordered = buttons

        fallback_idx: int | None = None
        for b in ordered:
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
