"""Dynamic salary expectation estimator using job scope + Glassdoor/market research.

Strictly bounded within the candidate's verified [salary_min, salary_max] band.
Never fabricates candidate history or exceeds configured limits.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

from bot.gemini.client import agy_ask, agy_available, extract_json_object
from bot.profile import CandidateProfile

logger = logging.getLogger("jobbot.appliers.salary")

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_SALARY_NUM_RE = re.compile(
    r"(?:R\$\s*|BRL\s*)?(\d{1,2}(?:[.,]\d{3})+|\d{4,5})(?:[.,]\d{2})?",
    re.IGNORECASE,
)

SALARY_SCHEMA = {
    "type": "object",
    "properties": {
        "recommended_brl": {"type": "integer"},
        "rationale": {"type": "string"},
        "market_source": {"type": "string"},
    },
    "required": ["recommended_brl", "rationale"],
}


@dataclass
class SalaryEstimate:
    amount_brl: int
    contract_type: str  # "clt" | "pj" | "unspecified"
    rationale: str
    glassdoor_snippets: list[str]


def _extract_job_salary_numbers(text: str) -> list[int]:
    """Extract plausible monthly BRL salary figures from job description or salary field."""
    nums: list[int] = []
    for m in _SALARY_NUM_RE.finditer(text or ""):
        raw = m.group(1).replace(".", "").replace(",", "")
        try:
            val = int(raw)
        except ValueError:
            continue
        if 2000 <= val <= 50000:
            nums.append(val)
    return nums


def search_glassdoor_snippets(company: str, title: str, *, timeout: int = 12) -> list[str]:
    """Best-effort public search for Glassdoor salary ranges for company + role.

    Uses public DuckDuckGo HTML search targeting glassdoor.com.br / glassdoor.com.
    Never attempts to bypass any CAPTCHA, Cloudflare gate, or login wall.
    """
    clean_company = (company or "").strip()
    clean_title = re.sub(r"[\(\)\[\]|/]+", " ", (title or "")).strip()
    if not clean_company or clean_company.lower() in ("confidencial", "confidential", "—", "-"):
        query = f"site:glassdoor.com.br salário {clean_title} Brasil"
    else:
        query = f"site:glassdoor.com.br {clean_company} {clean_title} salário"

    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Glassdoor public search unavailable: %s", exc)
        return []

    lower_html = html.lower()
    if "captcha" in lower_html or "anomaly-modal" in lower_html or "verify you are human" in lower_html:
        logger.debug("Search engine presented anti-bot check — skipping external salary lookup")
        return []

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    snippets: list[str] = []
    for node in soup.select(".result"):
        title_el = node.select_one(".result__a")
        snip_el = node.select_one(".result__snippet")
        t_txt = title_el.get_text(" ", strip=True) if title_el else ""
        s_txt = snip_el.get_text(" ", strip=True) if snip_el else ""
        combined = f"{t_txt} — {s_txt}".strip(" —")
        if combined and len(combined) > 20:
            snippets.append(combined[:280])
        if len(snippets) >= 5:
            break
    return snippets


class SalaryBudgetEstimator:
    """Estimates a realistic salary expectation inside [salary_min, salary_max] per job."""

    def __init__(self, profile: CandidateProfile, *, allow_web: bool = True, allow_ai: bool = True):
        self._profile = profile
        self._allow_web = allow_web
        self._allow_ai = allow_ai and agy_available()
        self._cache: dict[tuple[str, str], SalaryEstimate] = {}

    def estimate(self, job: dict, *, contract_type: str = "unspecified") -> SalaryEstimate:
        job_key = str(job.get("id") or job.get("url") or job.get("title") or "unknown")
        ctype = contract_type.lower() if contract_type else "unspecified"
        cache_key = (job_key, ctype)
        if cache_key in self._cache:
            return self._cache[cache_key]

        s_min = self._profile.salary_min or 6000
        s_max = self._profile.salary_max or 16000
        if s_max < s_min:
            s_max = s_min

        # 1. Check declared job salary first
        declared_text = f"{job.get('salary') or ''}\n{(job.get('body') or '')[:2500]}"
        declared_nums = _extract_job_salary_numbers(declared_text)

        # 2. Best-effort Glassdoor search
        snippets: list[str] = []
        if self._allow_web:
            snippets = search_glassdoor_snippets(job.get("company") or "", job.get("title") or "")

        # 3. Deterministic baseline inside [s_min, s_max] based on seniority & CLT/PJ
        baseline = self._heuristic_baseline(job, ctype, s_min, s_max, declared_nums)
        rationale = "Estimativa baseada no escopo/senioridade da vaga dentro da faixa configurada."

        # 4. Refine via Gemini 4 Neon Medium using Glassdoor snippets + job scope
        if self._allow_ai:
            ai_amount, ai_rationale = self._ask_ai_estimate(
                job=job,
                contract_type=ctype,
                s_min=s_min,
                s_max=s_max,
                declared_nums=declared_nums,
                snippets=snippets,
                baseline=baseline,
            )
            if ai_amount is not None:
                baseline = max(s_min, min(s_max, ai_amount))
                rationale = ai_rationale or rationale

        # Round nicely to nearest 500 BRL inside [s_min, s_max]
        rounded = int(round(baseline / 500.0) * 500)
        rounded = max(s_min, min(s_max, rounded))

        est = SalaryEstimate(
            amount_brl=rounded,
            contract_type=ctype,
            rationale=rationale,
            glassdoor_snippets=snippets,
        )
        self._cache[cache_key] = est
        logger.info(
            "salary expectation estimated",
            extra={
                "job_id": job_key,
                "contract_type": ctype,
                "amount_brl": rounded,
                "glassdoor_hits": len(snippets),
                "rationale": rationale[:180],
            },
        )
        return est

    @staticmethod
    def format_for_field(amount_brl: int, field_label: str = "", placeholder: str = "", field_type: str = "text") -> str:
        """Format salary value cleanly according to the target HTML input's period (hour/month/year) and mask."""
        ph = (placeholder or "").strip()
        lbl = (field_label or "").lower()

        value = amount_brl
        # Convert monthly rate to hourly (~168h/month) or annual (12x) if the field asks for it
        if re.search(r"\b(hora|hourly|por hora|valor[- ]?hora|/h)\b", lbl) or re.search(r"\b(hora|hourly|/h)\b", ph.lower()):
            value = max(35, int(round((amount_brl / 168.0) / 5.0) * 5))
        elif re.search(r"\b(anual|annual|yearly|per year|/ano)\b", lbl):
            value = amount_brl * 12

        if field_type == "number":
            return str(value)
        if "0,00" in ph or "0,00" in lbl:
            # Brazilian currency masked inputs (e.g. GeekHunter R$ 0,00): digits including cents
            return f"{value},00"
        if "r$" in ph.lower() or "r$" in lbl:
            return f"{value:,}".replace(",", ".")
        return str(value)

    @staticmethod
    def _heuristic_baseline(
        job: dict,
        contract_type: str,
        s_min: int,
        s_max: int,
        declared_nums: list[int],
    ) -> int:
        if declared_nums:
            avg = sum(declared_nums) // len(declared_nums)
            return max(s_min, min(s_max, avg))

        text = f"{job.get('title', '')} {job.get('seniority', '')} {(job.get('body') or '')[:800]}".lower()
        span = s_max - s_min
        if any(k in text for k in ("sênior", "senior", "sr", "staff", "lead", "tech lead", "arquitet", "principal")):
            ratio = 0.72
        elif any(k in text for k in ("júnior", "junior", "jr")):
            ratio = 0.15
        else:
            # Pleno / Mid-level default
            ratio = 0.45

        if contract_type == "pj":
            ratio = min(0.95, ratio + 0.18)
        elif contract_type == "clt":
            ratio = max(0.05, ratio - 0.08)

        return int(s_min + span * ratio)

    def _ask_ai_estimate(
        self,
        *,
        job: dict,
        contract_type: str,
        s_min: int,
        s_max: int,
        declared_nums: list[int],
        snippets: list[str],
        baseline: int,
    ) -> tuple[int | None, str]:
        snip_block = "\n".join(f"- {s}" for s in snippets) if snippets else "(nenhum resultado público do Glassdoor disponível)"
        prompt = (
            "Você é um consultor de remuneração de engenharia de software no Brasil.\n"
            f"O candidato aceita entre R$ {s_min} e R$ {s_max} mensais (faixa obrigatória: nunca ultrapasse esses limites).\n"
            "Considere: escopo da vaga, senioridade exigida, porte/budget estimado da empresa, modalidade (CLT vs PJ — PJ costuma ser ~25% maior que CLT) e dados do Glassdoor abaixo.\n"
            "Retorne APENAS JSON com `recommended_brl` (inteiro em reais/mês, múltiplo de 500) e `rationale` (curta em português).\n\n"
            f"Vaga: {job.get('title') or '—'}\n"
            f"Empresa: {job.get('company') or '—'}\n"
            f"Senioridade: {job.get('seniority') or '—'}\n"
            f"Tipo de contrato do campo: {contract_type}\n"
            f"Salário citado na vaga: {job.get('salary') or 'não informado'} (números detectados: {declared_nums})\n"
            f"Baseline heurístico: R$ {baseline}\n"
            f"Snippets Glassdoor:\n{snip_block}\n\n"
            f"Resumo da vaga:\n{(job.get('body') or '')[:1500]}"
        )
        try:
            raw = agy_ask(prompt, json_schema=SALARY_SCHEMA, timeout=45, max_retries=1)
            data = extract_json_object(raw) or {}
            rec = data.get("recommended_brl")
            if rec is not None:
                val = int(rec)
                if s_min <= val <= s_max:
                    return val, str(data.get("rationale") or "").strip()
                return max(s_min, min(s_max, val)), str(data.get("rationale") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("AI salary estimate fallback to heuristic: %s", exc)
        return None, ""
