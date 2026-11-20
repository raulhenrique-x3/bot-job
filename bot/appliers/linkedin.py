"""LinkedIn applier — handles multi-step Easy Apply and external ATS redirection."""

from __future__ import annotations

import logging
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    PlatformApplier,
    detect_active_captcha_on_page,
    detect_mfa_or_auth_wall,
    detect_submission_confirmation,
)
from bot.appliers.form_filler import PlaywrightFormFiller
from bot.profile import CandidateProfile

logger = logging.getLogger("jobbot.appliers.linkedin")


class LinkedInApplier(PlatformApplier):
    platform_name = "linkedin"

    def __init__(self, external_dispatcher: Callable | None = None):
        self._external_dispatcher = external_dispatcher

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return "linkedin.com" in (apply_url or "").lower()

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
                reason="LinkedIn requer sessão autenticada no navegador para candidatura.",
                dry_run=dry_run,
            )

        from bot.scrapers.browser import goto

        with goto(browser_context, apply_url, timeout_ms=timeout_seconds * 1000) as page:
            if page is None:
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=apply_url,
                    reason=f"Não foi possível carregar vaga no LinkedIn: {apply_url}",
                    retryable=True,
                    dry_run=dry_run,
                )

            current_url = page.url or apply_url
            body_text = page.inner_text("body") if page.locator("body").count() else ""

            if (
                "não aceita mais candidaturas" in body_text.lower()
                or "no longer accepting applications" in body_text.lower()
            ):
                return ApplicationOutcome(
                    status="expired",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason="Vaga encerrada no LinkedIn.",
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
                        reason=f"Sessão LinkedIn expirada ou checkpoint ativo: {check}",
                        dry_run=dry_run,
                    )

            # Find Apply / Easy Apply button (SDUI uses <a> for external apply and <button> for Easy Apply)
            apply_btn = page.locator(
                "button.jobs-apply-button, "
                "button:has-text('Candidatura simplificada'), "
                "button:has-text('Easy Apply'), "
                "a[href*='/safety/go/']:has-text('Candidatar-se'), "
                "a[href*='/safety/go/']:has-text('Apply'), "
                "button:has-text('Candidatar-se'), "
                "button:has-text('Apply'), "
                "a:has-text('Candidatar-se'), "
                "a:has-text('Apply')"
            ).first
            if apply_btn.count() == 0:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason="Botão de candidatura não encontrado no LinkedIn (vaga pode já estar aplicada ou exigir login).",
                    dry_run=dry_run,
                )

            btn_text = (apply_btn.inner_text(timeout=1500) or "").lower()
            is_easy = "simplificada" in btn_text or "easy apply" in btn_text

            if not is_easy:
                # Follow external ATS redirect if possible and delegate to registry
                ext_url = self._resolve_external_apply_url(page, browser_context, apply_btn)
                if ext_url and "linkedin.com" not in ext_url.lower() and self._external_dispatcher is not None:
                    logger.info("LinkedIn redirected to external ATS: %s", ext_url)
                    ext_job = dict(job)
                    ext_job["apply_url"] = ext_url
                    return self._external_dispatcher(
                        ext_job,
                        profile,
                        documents,
                        browser_context=browser_context,
                        dry_run=dry_run,
                        timeout_seconds=timeout_seconds,
                        cover_letter_provider=cover_letter_provider,
                    )
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=ext_url or current_url,
                    reason="Vaga no LinkedIn redireciona para candidatura externa. Continue pelo link com o currículo gerado.",
                    dry_run=dry_run,
                )

            modal_selector = (
                "dialog[data-testid='dialog'], [data-testid='dialog-content'], "
                ".jobs-easy-apply-modal, [role='dialog']"
            )
            try:
                apply_btn.click(timeout=5000)
                page.wait_for_selector(modal_selector, timeout=8000)
                # Wait for SDUI Easy Apply modal body (inputs/buttons) to finish hydrating
                try:
                    page.locator(modal_selector).first.locator("input, select, textarea, button[type='submit']").first.wait_for(
                        state="visible", timeout=6000
                    )
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(1200)
            except Exception as exc:  # noqa: BLE001
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason=f"Falha ao abrir modal Easy Apply: {exc}",
                    retryable=True,
                    dry_run=dry_run,
                )

            # Safety guard: ensure the logged-in LinkedIn session matches the candidate's real name
            # (prevents accidentally submitting under a scraper/burner LinkedIn account)
            mismatch_reason = self._check_account_identity(page, modal_selector, profile)
            if mismatch_reason:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform="linkedin_easy_apply",
                    apply_url=current_url,
                    reason=mismatch_reason,
                    dry_run=dry_run,
                )

            # Multi-step Easy Apply scoped strictly to the modal dialog
            filler = PlaywrightFormFiller(profile)
            outcome = filler.run_on_page(
                page,
                platform="linkedin_easy_apply",
                apply_url=current_url,
                job=job,
                documents=documents,
                dry_run=dry_run,
                timeout_seconds=timeout_seconds,
                cover_letter_provider=cover_letter_provider,
                scope_selector=modal_selector,
                max_steps=8,
            )
            if outcome.status == "applied":
                confirmed, conf_id = detect_submission_confirmation(page.url, page.inner_text("body"))
                if not confirmed:
                    outcome.status = "pending_human_action"
                    outcome.reason = "Easy Apply acionado sem confirmação explícita do LinkedIn."
                elif conf_id and not outcome.confirmation_id:
                    outcome.confirmation_id = conf_id
            return outcome

    @staticmethod
    def _check_account_identity(page, modal_selector: str, profile: CandidateProfile) -> str | None:
        """Verify Easy Apply session belongs to the candidate, not a burner scraper account."""
        first_expected = (profile.first_name or "").strip().lower()
        if not first_expected:
            return None
        try:
            modal = page.locator(modal_selector).first
            modal_text = (modal.inner_text(timeout=2000) or "")[:800].lower()
            # Check pre-filled first name input if present
            prefilled_first = ""
            for sel in ("input[aria-label*='First name' i]", "input[aria-label*='Nome' i]"):
                loc = modal.locator(sel).first
                if loc.count():
                    prefilled_first = (loc.input_value(timeout=1000) or "").strip().lower()
                    if prefilled_first:
                        break
            if not prefilled_first:
                # Fallback: inspect first text input value inside modal
                vals = page.evaluate(
                    """(sel) => {
                        const root = document.querySelector(sel);
                        if (!root) return [];
                        return [...root.querySelectorAll('input[type="text"]')].slice(0, 2).map(i => i.value || '');
                    }""",
                    modal_selector,
                )
                if vals and vals[0]:
                    prefilled_first = str(vals[0]).strip().lower()
            if prefilled_first and first_expected not in prefilled_first and prefilled_first not in first_expected:
                return (
                    f"Sessão LinkedIn autenticada pertence a outra conta ('{prefilled_first.title()}', "
                    f"esperado '{profile.full_name}'). Reautentique com sua conta pessoal via "
                    "'python -m bot.login linkedin' antes de enviar candidaturas Easy Apply."
                )
            if "contact info" in modal_text or "informações de contato" in modal_text:
                if first_expected not in modal_text and prefilled_first and first_expected not in prefilled_first:
                    return (
                        f"Conta LinkedIn ativa não corresponde ao perfil {profile.full_name}. "
                        "Reautentique com 'python -m bot.login linkedin'."
                    )
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _resolve_external_apply_url(page, browser_context, apply_btn) -> str:
        """Resolve external LinkedIn Apply URL from /safety/go/?url= href or popup/redirect."""
        try:
            href = apply_btn.get_attribute("href") or ""
            if "/safety/go/" in href:
                qs = parse_qs(urlparse(href).query)
                if qs.get("url"):
                    return unquote(qs["url"][0])
            if href.startswith("http") and "linkedin.com" not in href.lower():
                return href
        except Exception:  # noqa: BLE001
            pass
        try:
            with browser_context.expect_page(timeout=6000) as new_page_info:
                apply_btn.click(timeout=5000)
            new_page = new_page_info.value
            try:
                new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                url = new_page.url or ""
            finally:
                new_page.close()
            return url
        except Exception:  # noqa: BLE001
            # Fallback: check if current page navigated externally
            try:
                page.wait_for_timeout(1500)
                if page.url and "linkedin.com" not in page.url.lower():
                    return page.url
            except Exception:  # noqa: BLE001
                pass
            return ""
