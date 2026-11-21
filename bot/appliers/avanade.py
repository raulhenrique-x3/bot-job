"""Avanade Careers (Workday) multi-step application applier.

Avanade's careers portal routes all applications to Accenture/Avanade's Workday
instance (`accenture.wd103.myworkdayjobs.com/AvanadeCareers`).

Mapped Workday flow:
  Step 0: Start Your Application (Autofill with Resume / Apply Manually / Use My Last Application)
  Step 1: Create Account / Sign In (MANDATORY auth wall — Google or Email+Password; never bypassed)
  Step 2: Autofill with Resume (when using autofill path)
  Step 3: My Information
  Step 4: My Experience
  Step 5: Application Questions
  Step 6: Voluntary Disclosures
  Step 7: Review -> Submit

Requires a saved authenticated Playwright session in `state/auth_avanade.json`
created via `.venv/bin/python -m bot.login avanade`. When unauthenticated,
blocked by CAPTCHA, or facing an unverified required question, escalates safely
to `pending_human_action` so Telegram notifies the user with the direct link
and tailored resume PDF.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    FormField,
    HumanInterventionRequired,
    PlatformApplier,
    detect_active_captcha_on_page,
    detect_mfa_or_auth_wall,
    detect_recommendation_letter_request,
    detect_submission_confirmation,
    is_cover_letter_label,
)
from bot.appliers.form_filler import PlaywrightFormFiller, VerifiedFactAnswerer, _norm
from bot.profile import CandidateProfile

logger = logging.getLogger("jobbot.appliers.avanade")

_EXPIRED_MARKERS = (
    "job you are looking for is no longer available",
    "no longer accepting applications",
    "position has been filled",
    "page you are looking for doesn't exist",
    "page not found",
)

_WORKDAY_AUTH_SELECTORS = (
    "button[data-automation-id='GoogleSignInButton']",
    "button[data-automation-id='SignInWithEmailButton']",
    "button[data-automation-id='signInSubmitButton']",
    "button[data-automation-id='createAccountLink']",
)

_ALREADY_APPLIED_RE = re.compile(
    r"you have already applied|already applied for this job|application submitted|thank you for applying",
    re.IGNORECASE,
)


def _is_avanade_workday_url(url: str) -> bool:
    low = (url or "").lower()
    return "avanade.com" in low or ("myworkdayjobs.com" in low and "avanadecareers" in low)


def _extract_workday_dropdowns(page: Any) -> list[dict]:
    """Extract visible Workday custom listbox dropdown buttons (aria-haspopup='listbox')."""
    js = """() => {
        document.querySelectorAll('[data-jobbot-wd-dd]').forEach(e => e.removeAttribute('data-jobbot-wd-dd'));
        const out = [];
        let idx = 0;
        for (const btn of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
            if (btn.disabled) continue;
            if (btn.closest('header, nav, footer, [data-automation-id="utilityButtonBar"]')) continue;
            const style = window.getComputedStyle(btn);
            const rect = btn.getBoundingClientRect();
            if (style.display === 'none' || style.visibility === 'hidden' || rect.width <= 1 || rect.height <= 1) continue;

            let label = '';
            const ariaBy = btn.getAttribute('aria-labelledby') || '';
            if (ariaBy) {
                label = ariaBy.split(/\\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
            }
            if (!label) {
                const container = btn.closest('[data-automation-id^="formField"], fieldset, [role="group"]');
                if (container) {
                    const lbl = container.querySelector('label, legend');
                    if (lbl) label = lbl.innerText.trim();
                }
            }
            if (!label) {
                label = btn.getAttribute('aria-label') || btn.getAttribute('data-automation-id') || '';
            }
            const currentValue = (btn.innerText || '').trim();
            const required = Boolean(
                btn.getAttribute('aria-required') === 'true' ||
                label.includes('*')
            );
            idx += 1;
            const marker = `jb-wd-dd-${idx}`;
            btn.setAttribute('data-jobbot-wd-dd', marker);
            out.push({
                name: btn.getAttribute('data-automation-id') || btn.id || marker,
                label: label.replace(/\\s+/g, ' ').trim(),
                current_value: currentValue,
                required,
                selector: `[data-jobbot-wd-dd="${marker}"]`,
            });
        }
        return out;
    }"""
    try:
        return page.evaluate(js) or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("workday dropdown extraction failed: %s", exc)
        return []


def _current_workday_step(page: Any) -> str:
    """Read Workday's active progress bar step label (e.g. 'My Information', 'Review')."""
    js = """() => {
        const active = document.querySelector(
            '[data-automation-id="progressBarActiveStep"], [aria-current="step"]'
        );
        if (active && active.innerText) return active.innerText.replace(/\\s+/g, ' ').trim();
        const h2 = document.querySelector('main h2, h2[data-automation-id]');
        if (h2 && h2.innerText) return h2.innerText.replace(/\\s+/g, ' ').trim();
        return '';
    }"""
    try:
        return str(page.evaluate(js) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _read_workday_errors(page: Any) -> list[str]:
    js = """() => {
        const errs = [];
        for (const el of document.querySelectorAll('[data-automation-id="errorMessage"], [role="alert"]')) {
            const txt = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            if (txt && txt.length > 2 && !errs.includes(txt)) errs.push(txt);
        }
        return errs.slice(0, 5);
    }"""
    try:
        return page.evaluate(js) or []
    except Exception:  # noqa: BLE001
        return []


class AvanadeApplier(PlatformApplier):
    """Playwright applier for Avanade Careers on Workday (`AvanadeCareers`)."""

    platform_name = "avanade"

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return _is_avanade_workday_url(apply_url) or (job.get("source") or "").lower() == "avanade"

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
                status="pending_human_action",
                platform=self.platform_name,
                apply_url=apply_url,
                reason=(
                    "Avanade (Workday) requer sessão de navegador autenticada. "
                    "Execute `.venv/bin/python -m bot.login avanade` para salvar `state/auth_avanade.json`."
                ),
                dry_run=dry_run,
            )

        from bot.scrapers.browser import goto

        target_url = apply_url
        if "myworkdayjobs.com" in target_url.lower() and not target_url.lower().endswith("/apply"):
            if "/apply/" not in target_url.lower():
                target_url = f"{target_url.rstrip('/')}/apply"

        with goto(browser_context, target_url, timeout_ms=timeout_seconds * 1000) as page:
            if page is None:
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=target_url,
                    reason=f"Falha ao abrir página de candidatura Avanade/Workday: {target_url}",
                    retryable=True,
                    dry_run=dry_run,
                )

            return self._run_workday_flow(
                page,
                job=job,
                apply_url=target_url,
                profile=profile,
                documents=documents,
                dry_run=dry_run,
                timeout_seconds=timeout_seconds,
                cover_letter_provider=cover_letter_provider,
            )

    def _dismiss_cookies(self, page: Any) -> None:
        try:
            accept = page.locator("button[data-automation-id='legalNoticeAcceptButton']").first
            if accept.count() > 0 and accept.is_visible(timeout=1500):
                accept.click(timeout=3000)
                page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass

    def _wait_workday_ready(self, page: Any, timeout_ms: int = 15000) -> None:
        try:
            page.wait_for_function(
                """() => {
                    const loading = document.querySelector('[data-automation-id="loadingSpinner"], [aria-label="Loading"]');
                    if (loading && window.getComputedStyle(loading).display !== 'none') return false;
                    return Boolean(
                        document.querySelector('[data-automation-id="applyAdventurePage"]') ||
                        document.querySelector('[data-automation-id="autofillWithResume"]') ||
                        document.querySelector('[data-automation-id="applyManually"]') ||
                        document.querySelector('[data-automation-id="GoogleSignInButton"]') ||
                        document.querySelector('[data-automation-id="SignInWithEmailButton"]') ||
                        document.querySelector('[data-automation-id="signInSubmitButton"]') ||
                        document.querySelector('[data-automation-id="createAccountLink"]') ||
                        document.querySelector('[data-automation-id="bottom-navigation-next-button"]') ||
                        document.querySelector('[data-automation-id="file-upload-input-ref"]') ||
                        document.querySelector('[data-automation-id="errorPage"]') ||
                        document.querySelector('form input:not([type="hidden"])')
                    );
                }""",
                timeout=timeout_ms,
            )
        except Exception:  # noqa: BLE001
            page.wait_for_timeout(2000)

    def _wait_after_apply_choice(self, page: Any, timeout_ms: int = 15000) -> None:
        try:
            page.wait_for_function(
                """() => {
                    if (document.querySelector('[data-automation-id="applyAdventurePage"]')) return false;
                    return Boolean(
                        document.querySelector('[data-automation-id="GoogleSignInButton"]') ||
                        document.querySelector('[data-automation-id="SignInWithEmailButton"]') ||
                        document.querySelector('[data-automation-id="signInSubmitButton"]') ||
                        document.querySelector('[data-automation-id="createAccountLink"]') ||
                        document.querySelector('[data-automation-id="bottom-navigation-next-button"]') ||
                        document.querySelector('[data-automation-id="file-upload-input-ref"]')
                    );
                }""",
                timeout=timeout_ms,
            )
        except Exception:  # noqa: BLE001
            page.wait_for_timeout(2500)

    def _detect_workday_auth_wall(self, page: Any, body_text: str) -> str | None:
        current_url = page.url or ""
        for sel in _WORKDAY_AUTH_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=500):
                    return (
                        "Avanade (Workday) exige autenticação de conta (Google ou e-mail/senha). "
                        "Execute `.venv/bin/python -m bot.login avanade` para salvar a sessão em "
                        "`state/auth_avanade.json` ou finalize pelo link com o currículo em anexo."
                    )
            except Exception:  # noqa: BLE001
                continue
        step_label = _current_workday_step(page).lower()
        if any(k in step_label for k in ("create account", "sign in", "criar conta", "fazer login", "entrar")):
            return (
                "Avanade (Workday) parou na etapa obrigatória 'Create Account / Sign In'. "
                "Execute `.venv/bin/python -m bot.login avanade` para autenticar sua sessão."
            )
        generic_auth = detect_mfa_or_auth_wall(current_url, body_text)
        if generic_auth:
            return f"{generic_auth} Execute `.venv/bin/python -m bot.login avanade`."
        return None

    def _run_workday_flow(
        self,
        page: Any,
        *,
        job: dict,
        apply_url: str,
        profile: CandidateProfile,
        documents: ApplicationDocuments,
        dry_run: bool,
        timeout_seconds: int,
        cover_letter_provider: CoverLetterCallback | None,
    ) -> ApplicationOutcome:
        self._wait_workday_ready(page, timeout_ms=min(timeout_seconds * 1000, 15000))
        self._dismiss_cookies(page)

        current_url = page.url or apply_url
        body_text = page.inner_text("body") if page.locator("body").count() else ""
        head_low = f"{page.title() or ''}\n{body_text[:3000]}".lower()

        if any(m in head_low for m in _EXPIRED_MARKERS):
            return ApplicationOutcome(
                status="expired",
                platform=self.platform_name,
                apply_url=current_url,
                reason="Vaga encerrada ou indisponível na Avanade (Workday).",
                dry_run=dry_run,
            )

        if _ALREADY_APPLIED_RE.search(body_text[:3000]):
            return ApplicationOutcome(
                status="applied",
                platform=self.platform_name,
                apply_url=current_url,
                reason="Candidatura já registrada anteriormente na Avanade (Workday).",
                dry_run=dry_run,
            )

        cap = detect_active_captcha_on_page(page)
        if cap:
            return ApplicationOutcome(
                status="pending_human_action",
                platform=self.platform_name,
                apply_url=current_url,
                reason=cap,
                dry_run=dry_run,
            )

        # Step 0: "Start Your Application" chooser -> prefer Autofill with Resume, fallback Apply Manually
        for choice_sel in (
            "a[data-automation-id='autofillWithResume']",
            "a[data-automation-id='applyManually']",
            "a[data-automation-id='adventureButton']",
            "button:has-text('Apply')",
        ):
            btn = page.locator(choice_sel).first
            if btn.count() > 0:
                try:
                    if btn.is_visible(timeout=2000):
                        btn.click(timeout=5000)
                        self._wait_after_apply_choice(page, timeout_ms=15000)
                        break
                except Exception:  # noqa: BLE001
                    continue

        current_url = page.url or apply_url
        body_text = page.inner_text("body") if page.locator("body").count() else ""

        # Step 1: Mandatory Create Account / Sign In wall check
        auth_err = self._detect_workday_auth_wall(page, body_text)
        if auth_err:
            return ApplicationOutcome(
                status="pending_human_action",
                platform=self.platform_name,
                apply_url=current_url,
                reason=auth_err,
                dry_run=dry_run,
            )

        answerer = VerifiedFactAnswerer(profile)
        resume_excerpt = ""
        if documents.resume_md and Path(documents.resume_md).exists():
            resume_excerpt = Path(documents.resume_md).read_text(encoding="utf-8")[:3500]

        filled_fields: dict[str, str] = {}
        visited_steps: list[str] = []
        cover_letter_requested = False
        cover_pdf = documents.cover_letter_pdf
        cover_text = documents.cover_letter_text
        max_steps = 8

        for step_idx in range(1, max_steps + 1):
            self._wait_workday_ready(page, timeout_ms=10000)
            current_url = page.url or apply_url
            body_text = page.inner_text("body") if page.locator("body").count() else ""

            for check in (
                detect_active_captcha_on_page(page),
                self._detect_workday_auth_wall(page, body_text),
                detect_recommendation_letter_request(body_text),
            ):
                if check:
                    return ApplicationOutcome(
                        status="pending_human_action",
                        platform=self.platform_name,
                        apply_url=current_url,
                        reason=check,
                        cover_letter_requested=cover_letter_requested,
                        filled_fields=filled_fields,
                        dry_run=dry_run,
                    )

            step_name = _current_workday_step(page) or f"step-{step_idx}"
            if not visited_steps or visited_steps[-1] != step_name:
                visited_steps.append(step_name)

            # Extract standard HTML inputs + Workday custom dropdown buttons
            raw_fields = PlaywrightFormFiller._extract_fields(page)
            wd_dropdowns = _extract_workday_dropdowns(page)

            if any(is_cover_letter_label(f"{f.label} {f.name}") for f in raw_fields):
                cover_letter_requested = True
                if not cover_pdf and cover_letter_provider is not None:
                    cover_pdf, cover_text = cover_letter_provider()

            missing_required: list[str] = []
            try:
                # 1. Fill standard inputs / file uploads / checkboxes / radios / selects
                for fld in raw_fields:
                    key_label = fld.label or fld.name or fld.selector
                    if fld.field_type == "file":
                        low_lbl = f"{fld.label} {fld.name}".lower()
                        if is_cover_letter_label(low_lbl):
                            if cover_pdf and Path(cover_pdf).exists():
                                page.locator(fld.selector).first.set_input_files(cover_pdf)
                                filled_fields[key_label] = f"[FILE] {Path(cover_pdf).name}"
                                page.wait_for_timeout(1200)
                            elif fld.required:
                                missing_required.append(key_label)
                        elif documents.resume_pdf and Path(documents.resume_pdf).exists():
                            page.locator(fld.selector).first.set_input_files(documents.resume_pdf)
                            filled_fields[key_label] = f"[FILE] {Path(documents.resume_pdf).name}"
                            page.wait_for_timeout(1500)
                        continue

                    # Skip already pre-populated non-empty inputs (e.g. autofilled by Workday profile/resume)
                    if fld.field_type in ("text", "email", "tel", "url", "number", "textarea"):
                        try:
                            existing_val = (page.locator(fld.selector).first.input_value(timeout=500) or "").strip()
                            if existing_val:
                                continue
                        except Exception:  # noqa: BLE001
                            pass

                    answer = answerer.answer_field(
                        fld,
                        job=job,
                        resume_excerpt=resume_excerpt,
                        cover_letter_text=cover_text,
                    )
                    if answer is None or answer == "":
                        if fld.required:
                            missing_required.append(key_label)
                        continue
                    PlaywrightFormFiller._fill_control(page, fld, answer)
                    filled_fields[key_label] = answer[:120]

                # 2. Fill Workday custom listbox dropdown buttons
                for dd in wd_dropdowns:
                    cur = (dd.get("current_value") or "").strip().lower()
                    if cur and cur not in ("select one", "selecione", "selecione um", "--", "choose"):
                        continue
                    btn = page.locator(dd["selector"]).first
                    btn.click(timeout=3000)
                    page.wait_for_timeout(500)
                    opt_els = page.locator("[role='listbox'] [role='option'], [role='option']")
                    opt_cnt = min(opt_els.count(), 40)
                    options: list[str] = []
                    for i in range(opt_cnt):
                        try:
                            t = (opt_els.nth(i).inner_text(timeout=300) or "").strip()
                            if t:
                                options.append(t)
                        except Exception:  # noqa: BLE001
                            continue

                    fld = FormField(
                        name=dd["name"],
                        label=dd["label"],
                        field_type="select",
                        required=bool(dd["required"]),
                        selector=dd["selector"],
                        options=options,
                    )
                    answer = answerer.answer_field(
                        fld,
                        job=job,
                        resume_excerpt=resume_excerpt,
                        cover_letter_text=cover_text,
                    )
                    if not answer:
                        try:
                            page.keyboard.press("Escape")
                        except Exception:  # noqa: BLE001
                            pass
                        if fld.required:
                            missing_required.append(fld.label or fld.name)
                        continue

                    nans = _norm(answer)
                    clicked_opt = False
                    for i in range(opt_cnt):
                        o = opt_els.nth(i)
                        try:
                            otxt = _norm(o.inner_text(timeout=300) or "")
                            if nans and (nans == otxt or nans in otxt or otxt in nans):
                                o.click(timeout=2000)
                                clicked_opt = True
                                filled_fields[fld.label or fld.name] = answer[:120]
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    if not clicked_opt:
                        try:
                            page.keyboard.press("Escape")
                        except Exception:  # noqa: BLE001
                            pass
                        if fld.required:
                            missing_required.append(fld.label or fld.name)

            except HumanInterventionRequired as exc:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason=f"[Etapa: {step_name}] {exc.reason}",
                    cover_letter_requested=cover_letter_requested,
                    filled_fields=filled_fields,
                    dry_run=dry_run,
                )

            if missing_required:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason=(
                        f"[Etapa: {step_name}] Campos obrigatórios sem resposta verificada: "
                        f"{', '.join(missing_required[:5])}"
                    ),
                    cover_letter_requested=cover_letter_requested,
                    filled_fields=filled_fields,
                    dry_run=dry_run,
                )

            nav_btn = page.locator("button[data-automation-id='bottom-navigation-next-button']").first
            if nav_btn.count() == 0:
                break

            btn_text = (nav_btn.inner_text(timeout=1500) or "").strip()
            is_final_submit = bool(re.match(r"^(submit|enviar|finalizar)$", btn_text, re.IGNORECASE)) or (
                any(k in step_name.lower() for k in ("review", "revisar", "revisão"))
                and any(k in btn_text.lower() for k in ("submit", "enviar"))
            )

            if is_final_submit:
                if dry_run:
                    return ApplicationOutcome(
                        status="documents_ready",
                        platform=self.platform_name,
                        apply_url=current_url,
                        reason=(
                            f"[DRY-RUN] Fluxo Avanade/Workday validado até '{step_name}' "
                            f"(etapas: {' -> '.join(visited_steps)}; {len(filled_fields)} campos preenchidos), "
                            "envio não clicado."
                        ),
                        cover_letter_requested=cover_letter_requested,
                        filled_fields=filled_fields,
                        dry_run=True,
                    )

                try:
                    nav_btn.click(timeout=8000)
                    page.wait_for_timeout(3000)
                    self._wait_workday_ready(page, timeout_ms=12000)
                except Exception as exc:  # noqa: BLE001
                    return ApplicationOutcome(
                        status="application_failed",
                        platform=self.platform_name,
                        apply_url=current_url,
                        reason=f"Erro ao clicar em Submit na Avanade/Workday: {exc}",
                        retryable=True,
                        cover_letter_requested=cover_letter_requested,
                        filled_fields=filled_fields,
                        dry_run=False,
                    )

                post_url = page.url or current_url
                post_text = page.inner_text("body") if page.locator("body").count() else ""
                confirmed, conf_id = detect_submission_confirmation(post_url, post_text)
                if confirmed:
                    return ApplicationOutcome(
                        status="applied",
                        platform=self.platform_name,
                        apply_url=post_url,
                        confirmation_id=conf_id,
                        reason="Candidatura enviada na Avanade (Workday) com confirmação.",
                        cover_letter_requested=cover_letter_requested,
                        filled_fields=filled_fields,
                        dry_run=False,
                    )
                errs = _read_workday_errors(page)
                detail = f" Erros: {'; '.join(errs)}" if errs else ""
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=post_url,
                    reason=f"Submit acionado na Avanade/Workday sem confirmação explícita.{detail}",
                    cover_letter_requested=cover_letter_requested,
                    filled_fields=filled_fields,
                    dry_run=False,
                )

            # Intermediate step -> click "Save and Continue"
            try:
                nav_btn.click(timeout=6000)
                page.wait_for_timeout(1800)
                self._wait_workday_ready(page, timeout_ms=10000)
            except Exception as exc:  # noqa: BLE001
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason=f"Falha ao avançar etapa '{step_name}' na Avanade/Workday: {exc}",
                    cover_letter_requested=cover_letter_requested,
                    filled_fields=filled_fields,
                    dry_run=dry_run,
                )

            errs = _read_workday_errors(page)
            next_step_name = _current_workday_step(page) or f"step-{step_idx}"
            if errs and next_step_name == step_name:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=page.url or current_url,
                    reason=f"[Etapa: {step_name}] Erro de validação Workday: {'; '.join(errs[:3])}",
                    cover_letter_requested=cover_letter_requested,
                    filled_fields=filled_fields,
                    dry_run=dry_run,
                )

        if dry_run and filled_fields:
            return ApplicationOutcome(
                status="documents_ready",
                platform=self.platform_name,
                apply_url=page.url or current_url,
                reason=(
                    f"[DRY-RUN] Fluxo Avanade/Workday preenchido ({len(filled_fields)} campos; "
                    f"etapas: {' -> '.join(visited_steps)})."
                ),
                cover_letter_requested=cover_letter_requested,
                filled_fields=filled_fields,
                dry_run=True,
            )

        return ApplicationOutcome(
            status="pending_human_action",
            platform=self.platform_name,
            apply_url=page.url or current_url,
            reason=(
                "Fluxo Avanade/Workday não atingiu etapa final de revisão automaticamente "
                f"(etapas visitadas: {' -> '.join(visited_steps) or 'nenhuma'})."
            ),
            cover_letter_requested=cover_letter_requested,
            filled_fields=filled_fields,
            dry_run=dry_run,
        )
