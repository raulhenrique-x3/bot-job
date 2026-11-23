"""Wellfound (formerly AngelList Talent) in-platform application applier.

Requires an authenticated Wellfound session saved to `state/auth_wellfound.json`
via `.venv/bin/python -m bot.login wellfound`.
Fills the Wellfound short application note with the AI-tailored cover letter
(generated on demand via `cover_letter_provider`) and uploads the tailored resume
when a file input is present. Never bypasses CAPTCHA/DataDome/auth walls.
"""

from __future__ import annotations

import logging
import re

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    PlatformApplier,
    detect_active_captcha_on_page,
    detect_mfa_or_auth_wall,
    detect_submission_confirmation,
)
from bot.appliers.dom_ai import ask_ai_dom_recovery, resolve_locator, try_dismiss_overlay
from bot.profile import CandidateProfile

logger = logging.getLogger("jobbot.appliers.wellfound")

_EXPIRED_MARKERS = (
    "no longer accepting applications",
    "job is closed",
    "position has been filled",
    "this job has expired",
    "page not found",
    "404",
)
_ALREADY_APPLIED_RE = re.compile(r"\b(applied|you(?:'ve| have) applied|application sent)\b", re.IGNORECASE)
_WELLFOUND_NOTE_MAX_CHARS = 1000


def _trim_note(text: str, max_chars: int = _WELLFOUND_NOTE_MAX_CHARS) -> str:
    clean = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if len(clean) <= max_chars:
        return clean
    cutoff = clean[: max_chars - 3].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{cutoff}..."


class WellfoundApplier(PlatformApplier):
    platform_name = "wellfound"

    def can_handle(self, apply_url: str, job: dict) -> bool:
        low = (apply_url or "").lower()
        return "wellfound.com" in low or "angel.co" in low

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
                reason="Wellfound requer sessão de navegador autenticada (`python -m bot.login wellfound`).",
                dry_run=dry_run,
            )

        from bot.scrapers.browser import goto

        with goto(browser_context, apply_url, timeout_ms=timeout_seconds * 1000) as page:
            if page is None:
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=apply_url,
                    reason=f"Falha ao abrir vaga na Wellfound: {apply_url}",
                    retryable=True,
                    dry_run=dry_run,
                )

            current_url = page.url or apply_url
            body_text = page.inner_text("body") if page.locator("body").count() else ""
            title_low = (page.title() or "").lower()
            head_low = body_text[:3000].lower()

            if any(m in title_low or m in head_low for m in _EXPIRED_MARKERS):
                return ApplicationOutcome(
                    status="expired",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason="Vaga encerrada ou indisponível na Wellfound.",
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
                        reason=f"{check} Execute `.venv/bin/python -m bot.login wellfound` para autenticar.",
                        dry_run=dry_run,
                    )

            # Check if login button is prominently shown instead of authenticated apply flow
            login_link = page.locator("a[href*='/login']:has-text('Log in'), a[href*='/signup']:has-text('Sign up')").first
            if login_link.count() > 0 and page.locator("button:has-text('Apply')").count() == 0:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason="Sessão Wellfound não autenticada. Execute `.venv/bin/python -m bot.login wellfound` -> state/auth_wellfound.json.",
                    dry_run=dry_run,
                )

            # Check if already applied
            applied_badge = page.locator("button:has-text('Applied'), span:has-text('Applied')").first
            if applied_badge.count() > 0:
                try:
                    if applied_badge.is_visible(timeout=1500) and applied_badge.is_disabled():
                        return ApplicationOutcome(
                            status="applied",
                            platform=self.platform_name,
                            apply_url=current_url,
                            reason="Candidatura já realizada anteriormente na Wellfound.",
                            dry_run=dry_run,
                        )
                except Exception:  # noqa: BLE001
                    pass

            # Open Wellfound's in-platform Apply modal
            apply_btn = page.locator(
                "button[data-test='JobDetail-ApplyButton'], "
                "button:has-text('Apply Now'), "
                "button:has-text('Apply')"
            ).first
            if apply_btn.count() == 0:
                ai_cta = ask_ai_dom_recovery(
                    page,
                    goal="locate_apply_cta",
                    error_context="Botão Apply não encontrado na página da Wellfound.",
                    job=job,
                )
                if ai_cta:
                    if ai_cta.get("dismiss_overlay_selector"):
                        try_dismiss_overlay(page, ai_cta["dismiss_overlay_selector"])
                    alt_cta = resolve_locator(page, ai_cta.get("apply_cta_selector") or "")
                    if alt_cta is not None:
                        apply_btn = alt_cta
            if apply_btn.count() == 0:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason="Botão Apply não encontrado na página da Wellfound.",
                    dry_run=dry_run,
                )

            try:
                apply_btn.click(timeout=5000)
                page.wait_for_timeout(1500)
            except Exception as exc:  # noqa: BLE001
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason=f"Falha ao abrir modal de candidatura Wellfound: {exc}",
                    retryable=True,
                    dry_run=dry_run,
                )

            # Re-check if clicking Apply redirected to login/signup
            if detect_mfa_or_auth_wall(page.url or current_url, ""):
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=page.url or current_url,
                    reason="Wellfound redirecionou para login. Execute `.venv/bin/python -m bot.login wellfound`.",
                    dry_run=dry_run,
                )

            filled: dict[str, str] = {}
            cover_requested = False

            # Upload tailored resume PDF if file input exists in modal
            file_input = page.locator("input[type='file']").first
            if file_input.count() > 0 and documents.resume_pdf:
                try:
                    file_input.set_input_files(documents.resume_pdf)
                    filled["resume"] = documents.resume_pdf
                    page.wait_for_timeout(800)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Wellfound resume upload skipped/optional: %s", exc)

            # Fill Wellfound's signature short note / cover letter textarea
            note_box = page.locator(
                "textarea[name='userNote'], "
                "textarea[data-test*='Note'], "
                "textarea[placeholder*='note' i], "
                "textarea[placeholder*='message' i], "
                "textarea[placeholder*='interested' i], "
                "[role='dialog'] textarea, "
                "form textarea"
            ).first
            if note_box.count() > 0:
                cover_requested = True
                note_text = documents.cover_letter_text
                if not note_text and cover_letter_provider is not None:
                    try:
                        _, note_text = cover_letter_provider()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Wellfound cover letter generation failed: %s", exc)
                        note_text = ""
                if note_text:
                    trimmed = _trim_note(note_text)
                    try:
                        note_box.fill(trimmed, timeout=5000)
                        filled["userNote"] = f"({len(trimmed)} chars)"
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Failed to fill Wellfound note textarea: %s", exc)

            if dry_run:
                return ApplicationOutcome(
                    status="documents_ready",
                    platform=self.platform_name,
                    apply_url=page.url or current_url,
                    reason="Dry-run Wellfound concluído (modal preenchido, envio não clicado).",
                    cover_letter_requested=cover_requested,
                    filled_fields=filled,
                    dry_run=True,
                )

            submit_btn = page.locator(
                "button[data-test='JobApplicationModal-SubmitButton'], "
                "[role='dialog'] button[type='submit'], "
                "button:has-text('Send application'), "
                "button:has-text('Submit application'), "
                "button:has-text('Send Application')"
            ).first
            if submit_btn.count() == 0:
                ai_sub = ask_ai_dom_recovery(
                    page,
                    goal="locate_submit_button",
                    error_context="Botão final de envio ('Send application') não encontrado no modal da Wellfound.",
                    job=job,
                    scope_selector="[role='dialog']",
                )
                if ai_sub:
                    if ai_sub.get("dismiss_overlay_selector"):
                        try_dismiss_overlay(page, ai_sub["dismiss_overlay_selector"])
                    alt_sub = resolve_locator(
                        page,
                        ai_sub.get("submit_button_selector") or "",
                        scope_selector="[role='dialog']",
                    )
                    if alt_sub is not None:
                        submit_btn = alt_sub
            if submit_btn.count() == 0:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=page.url or current_url,
                    reason="Botão final de envio ('Send application') não encontrado no modal da Wellfound.",
                    cover_letter_requested=cover_requested,
                    filled_fields=filled,
                    dry_run=False,
                )

            try:
                if submit_btn.is_disabled():
                    return ApplicationOutcome(
                        status="pending_human_action",
                        platform=self.platform_name,
                        apply_url=page.url or current_url,
                        reason="Botão de envio na Wellfound está desabilitado (possível pergunta extra obrigatória).",
                        cover_letter_requested=cover_requested,
                        filled_fields=filled,
                        dry_run=False,
                    )
                submit_btn.click(timeout=5000)
                page.wait_for_timeout(2500)
            except Exception as exc:  # noqa: BLE001
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=page.url or current_url,
                    reason=f"Falha ao clicar em enviar na Wellfound: {exc}",
                    retryable=True,
                    cover_letter_requested=cover_requested,
                    filled_fields=filled,
                    dry_run=False,
                )

            post_text = page.inner_text("body") if page.locator("body").count() else ""
            confirmed, conf_id = detect_submission_confirmation(page.url or current_url, post_text)
            if confirmed or _ALREADY_APPLIED_RE.search(post_text[:3000]):
                return ApplicationOutcome(
                    status="applied",
                    platform=self.platform_name,
                    apply_url=page.url or current_url,
                    confirmation_id=conf_id,
                    reason="Candidatura enviada na Wellfound com sucesso.",
                    cover_letter_requested=cover_requested,
                    filled_fields=filled,
                    dry_run=False,
                )

            return ApplicationOutcome(
                status="pending_human_action",
                platform=self.platform_name,
                apply_url=page.url or current_url,
                reason="Clique de envio executado na Wellfound, mas confirmação não detectada explicitamente.",
                cover_letter_requested=cover_requested,
                filled_fields=filled,
                dry_run=False,
            )
