"""InHire ATS applier (*.inhire.app) — API inspection, native JobForm + Diversity + custom form submit."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    FormField,
    FormInspection,
    HumanInterventionRequired,
    PlatformApplier,
    detect_active_captcha_on_page,
    detect_mfa_or_auth_wall,
    detect_recommendation_letter_request,
    detect_sensitive_or_legal_field,
    detect_submission_confirmation,
    is_cover_letter_label,
    is_cpf_only_field,
)
from bot.appliers.dom_ai import ask_ai_dom_recovery, resolve_locator, try_dismiss_overlay
from bot.appliers.form_filler import PlaywrightFormFiller, VerifiedFactAnswerer
from bot.appliers.salary import SalaryBudgetEstimator
from bot.profile import CandidateProfile

logger = logging.getLogger("jobbot.appliers.inhire")

INHIRE_API_BASE = "https://api.inhire.app"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_RESERVED_SUBDOMAINS = frozenset({
    "api", "auth", "form-app", "forms", "forms-dev", "forms-prod", "embed", "files", "www", "mcp",
})
_INHIRE_PATH_RE = re.compile(r"^/(?:[^/]+/)?vagas/([0-9a-fA-F-]{8,})(?:/|$)", re.IGNORECASE)
_EXPIRED_PAGE_MARKERS = (
    "vaga encerrada",
    "vaga não encontrada",
    "vaga nao encontrada",
    "position closed",
    "não é válido",
    "nao e valido",
    "não está mais disponível",
    "nao esta mais disponivel",
    "inscrições encerradas",
    "inscricoes encerradas",
)


def _norm(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).strip()


def _first_visible(locator):
    for i in range(min(locator.count(), 15)):
        el = locator.nth(i)
        try:
            if el.is_visible():
                return el
        except Exception:  # noqa: BLE001
            continue
    return None


def parse_inhire_url(url: str) -> tuple[str, str] | None:
    """Extract (tenant, job_id) from a *.inhire.app job URL."""
    clean = (url or "").strip()
    if not clean:
        return None
    parsed = urlparse(clean)
    host = (parsed.netloc or "").lower().split(":")[0]
    if not host.endswith(".inhire.app"):
        return None
    tenant = host[: -len(".inhire.app")]
    if not tenant or "." in tenant or tenant in _RESERVED_SUBDOMAINS:
        return None
    m = _INHIRE_PATH_RE.match(parsed.path or "")
    if not m:
        return None
    return tenant, m.group(1)


def _inhire_get_json(path: str, tenant: str, timeout: int = 15) -> tuple[int, dict | None]:
    req = urllib.request.Request(
        f"{INHIRE_API_BASE}{path}",
        headers={
            "X-Tenant": tenant,
            "Accept": "application/json",
            "User-Agent": _UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body) if body else None
        except json.JSONDecodeError:
            return exc.code, None
    except Exception as exc:  # noqa: BLE001
        logger.debug("InHire GET %s (%s) failed: %s", path, tenant, exc)
        return 0, None


def _inhire_post_json(path: str, tenant: str, payload: dict, timeout: int = 20) -> tuple[int, dict | str | None]:
    req = urllib.request.Request(
        f"{INHIRE_API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "X-Tenant": tenant,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body) if body else body
        except json.JSONDecodeError:
            return exc.code, body
    except Exception as exc:  # noqa: BLE001
        logger.debug("InHire POST %s (%s) failed: %s", path, tenant, exc)
        return 0, str(exc)


def _map_custom_field(raw_field: dict) -> FormField:
    fid = str(raw_field.get("id") or raw_field.get("ref") or "")
    title = str(raw_field.get("title") or "").strip()
    raw_type = str(raw_field.get("type") or "short_text").lower()
    required = bool((raw_field.get("validations") or {}).get("required"))
    props = raw_field.get("properties") or {}
    choices = [
        str(c.get("label") or "").strip()
        for c in (props.get("choices") or [])
        if isinstance(c, dict) and str(c.get("label") or "").strip()
    ]

    if raw_type in ("multiple_choice", "dropdown"):
        ftype = "select"
    elif raw_type == "yes_no":
        ftype = "radio"
        if not choices:
            choices = ["Sim", "Não"]
    elif raw_type == "long_text":
        ftype = "textarea"
    elif raw_type in ("number", "numeric"):
        ftype = "number"
    elif raw_type == "email":
        ftype = "email"
    elif raw_type == "website":
        ftype = "url"
    elif raw_type == "file_upload":
        ftype = "file"
    else:
        ftype = "text"

    return FormField(
        name=fid,
        label=title,
        field_type=ftype,
        required=required,
        options=choices,
    )


def _match_choice_ids(answer: str, choices: list[dict], *, allow_multiple: bool) -> list[str]:
    """Match an answer string (or comma/semicolon-separated labels) against InHire choice objects."""
    if not answer or not choices:
        return []
    nans = _norm(answer)
    # 1. Exact full-label match
    for c in choices:
        if _norm(str(c.get("label") or "")) == nans:
            return [str(c["id"])]

    # 2. Multi-selection split or substring matching
    tokens = [_norm(t) for t in re.split(r"[,;\n|]+", answer) if _norm(t)] if allow_multiple else [nans]
    matched: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        for c in choices:
            cid = str(c.get("id") or "")
            clbl = _norm(str(c.get("label") or ""))
            if not cid or not clbl or cid in seen:
                continue
            if tok == clbl or tok in clbl or clbl in tok:
                seen.add(cid)
                matched.append(cid)
                if not allow_multiple:
                    return matched
    return matched


def _prepare_custom_form_answers(
    custom_form: dict,
    profile: CandidateProfile,
    *,
    job: dict,
    resume_excerpt: str = "",
    cover_letter_text: str = "",
    allow_ai: bool = True,
    allow_salary_web: bool = True,
) -> tuple[dict[str, str | list[str]], dict[str, str]]:
    """Resolve answers for an InHire Stage-2 custom subscription form.

    Returns (api_answers_by_field_id, human_readable_filled_fields).
    Raises HumanInterventionRequired if any required question cannot be safely answered.
    """
    fields = custom_form.get("fields") or []
    if not fields:
        return {}, {}

    answerer = VerifiedFactAnswerer(profile, allow_ai=allow_ai, allow_salary_web=allow_salary_web)
    api_answers: dict[str, str | list[str]] = {}
    display_fields: dict[str, str] = {}

    for rf in fields:
        if not isinstance(rf, dict):
            continue
        fid = str(rf.get("id") or "").strip()
        title = str(rf.get("title") or "").strip()
        raw_type = str(rf.get("type") or "short_text").lower()
        required = bool((rf.get("validations") or {}).get("required"))
        props = rf.get("properties") or {}
        choices = [
            c for c in (props.get("choices") or [])
            if isinstance(c, dict) and str(c.get("id") or "").strip() and str(c.get("label") or "").strip()
        ]
        allow_multi = bool(props.get("allow_multiple_selection"))

        if not fid:
            continue

        if raw_type == "file_upload":
            rec_err = detect_recommendation_letter_request(title)
            if rec_err:
                raise HumanInterventionRequired(rec_err, category="recommendation_letter")
            if required:
                raise HumanInterventionRequired(
                    f"Upload obrigatório no formulário adicional InHire: {title or fid}",
                    category="unverified_field",
                )
            continue

        fld = _map_custom_field(rf)
        ans = answerer.answer_field(
            fld,
            job=job,
            resume_excerpt=resume_excerpt,
            cover_letter_text=cover_letter_text,
        )
        if ans is None or ans == "":
            if required:
                raise HumanInterventionRequired(
                    f"Pergunta obrigatória sem resposta verificada no formulário InHire: {title or fid}",
                    category="unverified_field",
                )
            continue

        if raw_type in ("multiple_choice", "dropdown") and choices:
            matched_ids = _match_choice_ids(ans, choices, allow_multiple=allow_multi)
            if not matched_ids:
                if required:
                    raise HumanInterventionRequired(
                        f"Opção '{ans}' não encontrada na pergunta '{title}' do formulário InHire.",
                        category="select_mismatch",
                    )
                continue
            api_answers[fid] = matched_ids if allow_multi else matched_ids[0]
            id_to_lbl = {str(c["id"]): str(c["label"]).strip() for c in choices}
            display_fields[f"[Form] {title[:60]}"] = ", ".join(id_to_lbl[i] for i in matched_ids)
        elif raw_type == "yes_no":
            yn_val = "Sim" if _norm(ans) in ("sim", "yes", "true", "1") else "Não"
            api_answers[fid] = yn_val
            display_fields[f"[Form] {title[:60]}"] = yn_val
        else:
            val = str(ans).strip()
            api_answers[fid] = val
            display_fields[f"[Form] {title[:60]}"] = val[:120]

    return api_answers, display_fields


def _format_e164_phone(raw_phone: str) -> str:
    digits = re.sub(r"\D+", "", raw_phone or "")
    if not digits:
        return ""
    if (raw_phone or "").strip().startswith("+"):
        return f"+{digits}"
    if digits.startswith("55") and len(digits) in (12, 13):
        return f"+{digits}"
    if len(digits) in (10, 11):
        return f"+55{digits}"
    return f"+{digits}"


class InHireApplier(PlatformApplier):
    platform_name = "inhire"

    def can_handle(self, apply_url: str, job: dict) -> bool:
        low = (apply_url or "").lower()
        return "inhire.app" in low or "carreira.inhire.com.br" in low

    def inspect(self, job: dict, apply_url: str, browser_context=None) -> FormInspection:
        inspection = FormInspection(platform=self.platform_name, apply_url=apply_url)
        parsed = parse_inhire_url(apply_url)
        if not parsed:
            return inspection
        tenant, job_id = parsed

        status_code, page_data = _inhire_get_json(f"/job-posts/public/pages/{job_id}", tenant)
        if status_code == 404 or (
            isinstance(page_data, dict)
            and page_data.get("status") not in (None, "", "published")
        ):
            inspection.api_metadata = {"tenant": tenant, "job_id": job_id, "expired": True}
            return inspection

        meta: dict = {"tenant": tenant, "job_id": job_id, "expired": False}
        if isinstance(page_data, dict):
            meta.update({
                "title": page_data.get("displayName") or "",
                "workplace_type": page_data.get("workplaceType") or "",
                "contract_type": page_data.get("contractType") or [],
                "location": page_data.get("location") or "",
                "settings": page_data.get("settings") or {},
                "diversity": page_data.get("diversity"),
            })

        _, sub_data = _inhire_get_json(f"/forms/public/job-id/{job_id}/subscription", tenant)
        typeform_id = str(sub_data.get("typeformId") or "").strip() if isinstance(sub_data, dict) else ""
        if typeform_id:
            meta["typeform_id"] = typeform_id
            _, form_data = _inhire_get_json(f"/forms/public/form/{typeform_id}", tenant)
            if isinstance(form_data, dict):
                meta["custom_form"] = form_data
                for rf in form_data.get("fields") or []:
                    if not isinstance(rf, dict):
                        continue
                    fld = _map_custom_field(rf)
                    inspection.fields.append(fld)
                    rec_err = detect_recommendation_letter_request(fld.label)
                    if rec_err and not inspection.blocker_reason:
                        inspection.blocker_reason = rec_err
                        inspection.blocker_category = "recommendation_letter"
                    if is_cover_letter_label(fld.label):
                        inspection.cover_letter_requested = True
                        if fld.required:
                            inspection.cover_letter_required = True
                    if fld.required:
                        sens_err = detect_sensitive_or_legal_field(fld.label)
                        if sens_err and not inspection.blocker_reason:
                            inspection.blocker_reason = sens_err
                            inspection.blocker_category = "legal_or_sensitive"

        inspection.api_metadata = meta
        return inspection

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
        inspection = self.inspect(job, apply_url, browser_context=browser_context)
        meta = inspection.api_metadata or {}
        if meta.get("expired"):
            return ApplicationOutcome(
                status="expired",
                platform=self.platform_name,
                apply_url=apply_url,
                reason="Vaga encerrada ou indisponível no InHire.",
                dry_run=dry_run,
            )

        if inspection.blocker_reason:
            cpf_only_ok = (
                inspection.blocker_category == "legal_or_sensitive"
                and bool(profile.cpf)
                and bool(inspection.fields)
                and all(
                    is_cpf_only_field(f.label) or not detect_sensitive_or_legal_field(f.label)
                    for f in inspection.fields
                    if f.required
                )
            )
            if not cpf_only_ok:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=apply_url,
                    reason=inspection.blocker_reason,
                    cover_letter_requested=inspection.cover_letter_requested,
                    dry_run=dry_run,
                )

        cover_pdf = documents.cover_letter_pdf
        cover_text = documents.cover_letter_text
        if inspection.cover_letter_requested and not cover_pdf and cover_letter_provider is not None:
            cover_pdf, cover_text = cover_letter_provider()

        resume_excerpt = ""
        if documents.resume_md and Path(documents.resume_md).exists():
            resume_excerpt = Path(documents.resume_md).read_text(encoding="utf-8")[:3500]

        custom_answers: dict[str, str | list[str]] = {}
        custom_display_fields: dict[str, str] = {}
        custom_form = meta.get("custom_form")
        if isinstance(custom_form, dict):
            try:
                custom_answers, custom_display_fields = _prepare_custom_form_answers(
                    custom_form,
                    profile,
                    job=job,
                    resume_excerpt=resume_excerpt,
                    cover_letter_text=cover_text,
                )
            except HumanInterventionRequired as exc:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=apply_url,
                    reason=exc.reason,
                    cover_letter_requested=inspection.cover_letter_requested,
                    dry_run=dry_run,
                )

        if browser_context is None:
            return ApplicationOutcome(
                status="application_failed",
                platform=self.platform_name,
                apply_url=apply_url,
                reason="Contexto de navegador indisponível para formulário InHire.",
                retryable=True,
                dry_run=dry_run,
            )

        from bot.scrapers.browser import goto

        with goto(browser_context, apply_url, timeout_ms=timeout_seconds * 1000) as page:
            if page is None:
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=apply_url,
                    reason=f"Não foi possível carregar página InHire: {apply_url}",
                    retryable=True,
                    dry_run=dry_run,
                )

            return self._apply_on_page(
                page,
                job=job,
                apply_url=apply_url,
                profile=profile,
                documents=documents,
                meta=meta,
                custom_answers=custom_answers,
                custom_display_fields=custom_display_fields,
                cover_letter_requested=inspection.cover_letter_requested,
                cover_letter_provider=cover_letter_provider,
                dry_run=dry_run,
                timeout_seconds=timeout_seconds,
            )

    def _apply_on_page(
        self,
        page,
        *,
        job: dict,
        apply_url: str,
        profile: CandidateProfile,
        documents: ApplicationDocuments,
        meta: dict,
        custom_answers: dict[str, str | list[str]],
        custom_display_fields: dict[str, str],
        cover_letter_requested: bool,
        cover_letter_provider: CoverLetterCallback | None,
        dry_run: bool,
        timeout_seconds: int,
        allow_ai: bool = True,
        allow_salary_web: bool = True,
    ) -> ApplicationOutcome:
        try:
            page.wait_for_selector(
                "form[data-component-name='JobForm'], "
                "button:has-text('Candidatar-se para a vaga'), "
                "button:has-text('Apply for the job'), "
                "input[name='name'], input[type='email']",
                timeout=10000,
            )
        except Exception:  # noqa: BLE001
            page.wait_for_timeout(2000)

        current_url = page.url or apply_url
        body_text = page.inner_text("body") if page.locator("body").count() else ""
        if any(m in body_text.lower() for m in _EXPIRED_PAGE_MARKERS):
            return ApplicationOutcome(
                status="expired",
                platform=self.platform_name,
                apply_url=current_url,
                reason="Vaga encerrada ou indisponível no InHire.",
                dry_run=dry_run,
            )

        for check in (
            detect_active_captcha_on_page(page),
            detect_mfa_or_auth_wall(current_url, body_text),
        ):
            if check:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason=check,
                    dry_run=dry_run,
                )

        # Open JobForm if only the overview CTA is visible
        if page.locator("form[data-component-name='JobForm'], input[name='name'], input[type='email']").count() == 0:
            apply_btn = _first_visible(
                page.locator(
                    "button:has-text('Candidatar-se para a vaga'), "
                    "button:has-text('Apply for the job'), "
                    "a:has-text('Candidatar-se')"
                )
            )
            if apply_btn is not None:
                try:
                    apply_btn.click(timeout=5000)
                    page.wait_for_timeout(1500)
                except Exception:  # noqa: BLE001
                    pass

        job_form = page.locator("form[data-component-name='JobForm']").first
        if job_form.count() == 0:
            filler = PlaywrightFormFiller(profile, allow_ai_qa=allow_ai, allow_salary_web=allow_salary_web)
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

        filled_fields: dict[str, str] = dict(custom_display_fields)
        try:
            self._fill_job_form_tab1(
                page,
                job_form,
                job=job,
                profile=profile,
                documents=documents,
                filled_fields=filled_fields,
                allow_ai=allow_ai,
                allow_salary_web=allow_salary_web,
            )
            self._handle_diversity_tab_if_present(page, job_form, filled_fields=filled_fields)
        except HumanInterventionRequired as exc:
            return ApplicationOutcome(
                status="pending_human_action",
                platform=self.platform_name,
                apply_url=page.url or apply_url,
                reason=exc.reason,
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=dry_run,
            )

        submit_btn = job_form.locator("button[type='submit']").first
        if submit_btn.count() == 0:
            ai_sub = ask_ai_dom_recovery(
                page,
                goal="locate_submit_button",
                error_context="Botão de envio não encontrado no formulário InHire.",
                job=job,
                allow_ai=allow_ai,
            )
            if ai_sub:
                if ai_sub.get("dismiss_overlay_selector"):
                    try_dismiss_overlay(page, ai_sub["dismiss_overlay_selector"])
                alt_sub = resolve_locator(page, ai_sub.get("submit_button_selector") or "")
                if alt_sub is not None:
                    submit_btn = alt_sub
        if submit_btn.count() == 0:
            return ApplicationOutcome(
                status="pending_human_action",
                platform=self.platform_name,
                apply_url=page.url or apply_url,
                reason="Botão de envio não encontrado no formulário InHire.",
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=dry_run,
            )

        # Wait briefly for async resume upload or React validation to enable submit button
        for _ in range(8):
            try:
                if not submit_btn.is_disabled():
                    break
            except Exception:  # noqa: BLE001
                break
            page.wait_for_timeout(400)

        try:
            if submit_btn.is_disabled():
                errs = self._collect_assistive_errors(job_form)
                detail = f" Erros: {'; '.join(errs)}" if errs else ""
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=page.url or apply_url,
                    reason=f"Botão de envio permaneceu desabilitado no InHire.{detail}",
                    cover_letter_requested=cover_letter_requested,
                    filled_fields=filled_fields,
                    dry_run=dry_run,
                )
        except Exception:  # noqa: BLE001
            pass

        if dry_run:
            return ApplicationOutcome(
                status="documents_ready",
                platform=self.platform_name,
                apply_url=page.url or apply_url,
                reason=f"[DRY-RUN] Formulário InHire preenchido e validado ({len(filled_fields)} campos), envio não executado.",
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=True,
            )

        return self._submit_and_confirm(
            page,
            submit_btn,
            apply_url=apply_url,
            meta=meta,
            custom_answers=custom_answers,
            cover_letter_requested=cover_letter_requested,
            filled_fields=filled_fields,
            timeout_seconds=timeout_seconds,
        )

    def _fill_job_form_tab1(
        self,
        page,
        form,
        *,
        job: dict,
        profile: CandidateProfile,
        documents: ApplicationDocuments,
        filled_fields: dict[str, str],
        allow_ai: bool,
        allow_salary_web: bool,
    ) -> None:
        answerer = VerifiedFactAnswerer(profile, allow_ai=allow_ai, allow_salary_web=allow_salary_web)

        # 1. CPF / Document field if enabled by tenant
        cpf_input = form.locator("input[name='document.value'], input[name='document.cpf']").first
        if cpf_input.count() > 0 and cpf_input.is_visible() and not cpf_input.is_disabled():
            cpf_ans = answerer.answer_field(FormField("document.value", "CPF", "text", True), job=job)
            if cpf_ans:
                cpf_input.fill(cpf_ans)
                filled_fields["CPF"] = "[CONFIRMED]"

        # 2. CEP field if enabled instead of country/district
        cep_input = form.locator("input[name='cep']").first
        if cep_input.count() > 0 and cep_input.is_visible():
            cep_val = str((profile.raw or {}).get("cep") or "").strip()
            if not cep_val:
                cep_val = answerer.answer_field(FormField("cep", "CEP", "text", True), job=job) or ""
            if cep_val:
                cep_input.fill(cep_val)
                filled_fields["CEP"] = cep_val
                page.wait_for_timeout(1200)

        # 3. Basic identity fields
        name_in = form.locator("#name, input[name='name']").first
        if name_in.count() > 0 and name_in.is_visible() and profile.full_name:
            name_in.fill(profile.full_name)
            filled_fields["Nome completo"] = profile.full_name

        email_in = form.locator("#email, input[name='email']").first
        if email_in.count() > 0 and email_in.is_visible() and profile.email:
            email_in.fill(profile.email)
            filled_fields["Email"] = profile.email

        phone_in = form.locator("#phone, input[name='phone']").first
        e164_phone = _format_e164_phone(profile.phone)
        if phone_in.count() > 0 and phone_in.is_visible() and e164_phone:
            phone_in.click(timeout=3000)
            phone_in.fill(e164_phone)
            filled_fields["Telefone"] = e164_phone

        li_in = form.locator("#linkedinUsername, input[name='linkedinUsername']").first
        if li_in.count() > 0 and li_in.is_visible() and profile.linkedin_url:
            li_in.fill(profile.linkedin_url)
            filled_fields["LinkedIn"] = profile.linkedin_url

        # 4. Country (.react-dropdown-select)
        country_dd = form.locator("[data-component-name='Dropdown'][name='country']").first
        if country_dd.count() > 0 and country_dd.is_visible():
            self._select_inhire_dropdown(
                page,
                country_dd,
                preferred_value="BR",
                search_text="Brasil",
            )
            filled_fields["País"] = "Brasil (BR)"
            page.wait_for_timeout(400)

        # 5. City (districtBr dropdown after BR selected, or plain #district input)
        district_br_dd = form.locator("[data-component-name='Dropdown'][name='districtBr']").first
        city = (profile.city or "Recife").strip()
        state = (profile.state or "").strip()
        city_target = f"{city} - {state}" if state else city
        if district_br_dd.count() > 0 and district_br_dd.is_visible():
            chosen_city = self._select_inhire_dropdown(
                page,
                district_br_dd,
                preferred_label=city_target,
                search_text=city,
            )
            filled_fields["Cidade"] = chosen_city or city_target
        else:
            district_in = form.locator("#district, input[name='district']").first
            if district_in.count() > 0 and district_in.is_visible() and not district_in.is_disabled():
                district_in.fill(city_target)
                filled_fields["Cidade"] = city_target

        # 6. Work model & Referral radios
        work_yes = form.locator("input[name='workModel'][value='true']").first
        if work_yes.count() > 0:
            try:
                work_yes.check(force=True)
                filled_fields["Modelo de trabalho"] = "Sim"
            except Exception:  # noqa: BLE001
                pass

        ind_no = form.locator("input[name='isIndication'][value='false']").first
        if ind_no.count() > 0:
            try:
                ind_no.check(force=True)
            except Exception:  # noqa: BLE001
                pass

        # 7. Resume file upload (hidden <input type="file" name="resume">)
        resume_in = form.locator("input[type='file'][name='resume'], input[type='file']").first
        if resume_in.count() > 0 and documents.resume_pdf and Path(documents.resume_pdf).exists():
            resume_in.set_input_files(documents.resume_pdf)
            filled_fields["Currículo"] = f"[FILE] {Path(documents.resume_pdf).name}"
            # Wait briefly for InHire's async S3 presigned upload to complete
            for _ in range(10):
                page.wait_for_timeout(400)
                if form.locator("input[type='file'][name='resume']").count() == 0:
                    break

        # 8. Salary expectation (moneyBRCents mask expects cents e.g. 10000,00 -> 10000)
        sal_in = form.locator("#salaryExpectation, input[name='salaryExpectation']").first
        if sal_in.count() > 0 and sal_in.is_visible():
            sal_field_box = sal_in.locator("xpath=ancestor::*[@data-component-name='Field'][1]")
            sal_label = sal_field_box.inner_text() if sal_field_box.count() > 0 else "Pretensão salarial"
            sal_lower = sal_label.lower()
            ctype = "pj" if "pj" in sal_lower else ("clt" if "clt" in sal_lower else "unspecified")
            est = SalaryBudgetEstimator(profile, allow_web=allow_salary_web, allow_ai=allow_ai).estimate(
                job, contract_type=ctype
            )
            formatted_sal = f"{est.amount_brl},00"
            sal_in.fill(formatted_sal)
            filled_fields["Pretensão salarial"] = f"R$ {formatted_sal}"

        # 9. Contract type dropdown (appears when job accepts multiple contract types, e.g. CLT and PJ)
        contract_dd = form.locator("[data-component-name='Dropdown'][name='contractType']").first
        if contract_dd.count() > 0 and contract_dd.is_visible():
            pref = "PJ" if profile.has_cnpj else "CLT"
            chosen_ct = self._select_inhire_dropdown(page, contract_dd, preferred_label=pref)
            if chosen_ct:
                filled_fields["Tipo de contrato"] = chosen_ct

        # 10. Privacy checkbox if visible on Tab 1
        priv_cb = form.locator("input[name='privacyPolicy']").first
        if priv_cb.count() > 0 and priv_cb.is_visible():
            try:
                priv_cb.check(force=True)
                filled_fields["Política de Privacidade"] = "true"
            except Exception:  # noqa: BLE001
                pass

    def _handle_diversity_tab_if_present(self, page, form, *, filled_fields: dict[str, str]) -> None:
        next_btn = _first_visible(
            form.locator("button:has-text('Avançar'), button:has-text('Próximo'), button:has-text('Next')")
        )
        if next_btn is None:
            return

        for _ in range(8):
            try:
                if not next_btn.is_disabled():
                    break
            except Exception:  # noqa: BLE001
                break
            page.wait_for_timeout(400)

        if next_btn.is_disabled():
            errs = self._collect_assistive_errors(form)
            detail = f" Erros: {'; '.join(errs)}" if errs else ""
            raise HumanInterventionRequired(
                f"Não foi possível avançar da aba 1 no InHire (campos pendentes).{detail}",
                category="validation_error",
            )

        next_btn.click(timeout=5000)
        page.wait_for_timeout(600)

        # In Tab 2, prefer "Prefiro não responder" / "Prefer not to" when required diversity fields are present
        prefer_not_cb = form.locator(
            "label:has-text('Prefiro não responder') input[type='checkbox'], "
            "label:has-text('Prefer not to') input[type='checkbox']"
        ).first
        if prefer_not_cb.count() > 0:
            try:
                prefer_not_cb.check(force=True)
                filled_fields["Grupo de diversidade"] = "Prefiro não responder"
            except Exception:  # noqa: BLE001
                pass

        div_dropdowns = form.locator("[data-component-name='Dropdown'][name^='questionsDiversity.']")
        for i in range(min(div_dropdowns.count(), 8)):
            dd = div_dropdowns.nth(i)
            try:
                if not dd.is_visible():
                    continue
                dd_name = (dd.get_attribute("name") or "").lower()
                if "peoplewithdisability" in dd_name:
                    self._select_inhire_dropdown(page, dd, preferred_label="Não")
                else:
                    self._select_inhire_dropdown(page, dd, preferred_label="Prefiro não responder")
            except Exception:  # noqa: BLE001
                continue

        priv_cb = form.locator("#privacyPolicy, input[name='privacyPolicy']").first
        if priv_cb.count() > 0:
            try:
                priv_cb.check(force=True)
                filled_fields["Política de Privacidade"] = "true"
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _select_inhire_dropdown(
        page,
        dropdown_loc,
        *,
        preferred_value: str = "",
        preferred_label: str = "",
        search_text: str = "",
    ) -> str:
        trigger = dropdown_loc.locator(".react-dropdown-select").first
        if trigger.count() == 0:
            trigger = dropdown_loc
        trigger.click(timeout=3000)
        page.wait_for_timeout(300)

        if search_text:
            search_in = dropdown_loc.locator("[data-component-name='DropdownOptionsSearch'] input").first
            if search_in.count() > 0 and search_in.is_visible():
                search_in.fill(search_text)
                page.wait_for_timeout(450)

        if preferred_value:
            val_opt = dropdown_loc.locator(
                f"[data-component-name='DropdownOption'][data-option-value='{preferred_value}']"
            ).first
            if val_opt.count() > 0:
                label = (val_opt.get_attribute("aria-label") or val_opt.inner_text() or preferred_value).strip()
                val_opt.click(timeout=3000)
                return label

        opts = dropdown_loc.locator("[data-component-name='DropdownOption']")
        cnt = min(opts.count(), 40)
        if cnt == 0:
            return ""

        npref = _norm(preferred_label)
        chosen_loc = None
        chosen_lbl = ""
        if npref:
            for i in range(cnt):
                o = opts.nth(i)
                lbl = (o.get_attribute("aria-label") or o.inner_text() or "").strip()
                nlbl = _norm(lbl)
                if npref == nlbl or npref in nlbl:
                    chosen_loc = o
                    chosen_lbl = lbl
                    break
        if chosen_loc is None:
            chosen_loc = opts.first
            chosen_lbl = (chosen_loc.get_attribute("aria-label") or chosen_loc.inner_text() or "").strip()

        chosen_loc.click(timeout=3000)
        return chosen_lbl

    @staticmethod
    def _collect_assistive_errors(form) -> list[str]:
        errs: list[str] = []
        nodes = form.locator("[data-component-name='AssistiveContent']")
        for i in range(min(nodes.count(), 15)):
            try:
                txt = (nodes.nth(i).inner_text() or "").strip()
                if txt:
                    errs.append(txt)
            except Exception:  # noqa: BLE001
                pass
        return errs

    def _submit_and_confirm(
        self,
        page,
        submit_btn,
        *,
        apply_url: str,
        meta: dict,
        custom_answers: dict[str, str | list[str]],
        cover_letter_requested: bool,
        filled_fields: dict[str, str],
        timeout_seconds: int,
    ) -> ApplicationOutcome:
        captured: dict = {"status": 0, "data": None}

        def _on_response(resp) -> None:
            try:
                if "/job-talents/public/" in (resp.url or "") and resp.request.method == "POST":
                    captured["status"] = resp.status
                    try:
                        captured["data"] = resp.json()
                    except Exception:  # noqa: BLE001
                        captured["data"] = resp.text()
            except Exception:  # noqa: BLE001
                pass

        if hasattr(page, "on"):
            page.on("response", _on_response)

        try:
            submit_btn.scroll_into_view_if_needed(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        try:
            submit_btn.click(timeout=8000)
        except Exception as exc:  # noqa: BLE001
            return ApplicationOutcome(
                status="application_failed",
                platform=self.platform_name,
                apply_url=page.url or apply_url,
                reason=f"Erro ao clicar no botão de envio no InHire: {exc}",
                retryable=True,
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=False,
            )

        post_url = page.url or apply_url
        post_text = ""
        confirmed = False
        conf_id = ""
        talent_id = ""
        max_wait_ms = min(timeout_seconds * 1000, 12000)
        waited_ms = 0
        while waited_ms < max_wait_ms:
            page.wait_for_timeout(500)
            waited_ms += 500
            if isinstance(captured.get("data"), dict) and captured["data"].get("talentId"):
                talent_id = str(captured["data"]["talentId"])
            try:
                post_url = page.url or apply_url
                post_text = page.inner_text("body") if page.locator("body").count() else ""
            except Exception:  # noqa: BLE001
                continue
            confirmed, conf_id = detect_submission_confirmation(post_url, post_text)
            if talent_id or (confirmed and not (meta.get("typeform_id") and custom_answers)):
                break
            if any(
                m in post_text.lower()
                for m in ("já participa da vaga", "intervalo mínimo entre candidaturas", "ja participa da vaga")
            ):
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=post_url,
                    reason="Candidatura recente já registrada anteriormente nesta vaga no InHire (bloqueio de reinscrição).",
                    cover_letter_requested=cover_letter_requested,
                    filled_fields=filled_fields,
                    dry_run=False,
                )
            if captured["status"] >= 400:
                break

        if not talent_id and captured["status"] >= 400:
            err_msg = ""
            if isinstance(captured.get("data"), dict):
                err_msg = str(captured["data"].get("message") or captured["data"])
            else:
                err_msg = str(captured.get("data") or "")
            return ApplicationOutcome(
                status="pending_human_action",
                platform=self.platform_name,
                apply_url=post_url,
                reason=f"InHire recusou o envio (HTTP {captured['status']}): {err_msg[:200]}",
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=False,
            )

        # Stage 2: submit custom subscription form via API when present
        typeform_id = str(meta.get("typeform_id") or "")
        tenant = str(meta.get("tenant") or "")
        job_id = str(meta.get("job_id") or "")
        if typeform_id and custom_answers and talent_id and tenant and job_id:
            sub_status, sub_resp = _inhire_post_json(
                "/forms/form/submit",
                tenant,
                {
                    "talentId": talent_id,
                    "formId": typeform_id,
                    "jobId": job_id,
                    "type": "subscription",
                    "answers": custom_answers,
                },
            )
            if sub_status not in (200, 201, 204):
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=post_url,
                    confirmation_id=talent_id,
                    reason=(
                        f"Inscrição inicial registrada (talentId={talent_id}), mas falhou o envio "
                        f"do formulário adicional (HTTP {sub_status}: {str(sub_resp)[:160]})."
                    ),
                    cover_letter_requested=cover_letter_requested,
                    filled_fields=filled_fields,
                    dry_run=False,
                )

        if talent_id or confirmed:
            return ApplicationOutcome(
                status="applied",
                platform=self.platform_name,
                apply_url=post_url,
                confirmation_id=talent_id or conf_id,
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=False,
            )

        return ApplicationOutcome(
            status="pending_human_action",
            platform=self.platform_name,
            apply_url=post_url,
            reason="Envio acionado no InHire, mas não houve confirmação explícita nem talentId retornado.",
            cover_letter_requested=cover_letter_requested,
            filled_fields=filled_fields,
            dry_run=False,
        )
