"""Gupy portal applier — clicks Candidatar-se, checks auth/SSO, respects proprietary flows."""

from __future__ import annotations

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    PlatformApplier,
    detect_active_captcha_on_page,
    detect_mfa_or_auth_wall,
)
from bot.appliers.dom_ai import ask_ai_dom_recovery, resolve_locator, try_dismiss_overlay
from bot.appliers.form_filler import PlaywrightFormFiller
from bot.profile import CandidateProfile


class GupyApplier(PlatformApplier):
    platform_name = "gupy"

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return "gupy.io" in (apply_url or "").lower()

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
                reason="Gupy requer conta autenticada e fluxo interativo. Continue pelo link com o currículo personalizado em anexo.",
                dry_run=dry_run,
            )

        from bot.scrapers.browser import goto

        with goto(browser_context, apply_url, timeout_ms=timeout_seconds * 1000) as page:
            if page is None:
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=apply_url,
                    reason=f"Não foi possível acessar vaga na Gupy: {apply_url}",
                    retryable=True,
                    dry_run=dry_run,
                )

            try:
                page.wait_for_selector("button:has-text('Candidatar'), form input", timeout=8000)
            except Exception:  # noqa: BLE001
                pass

            current_url = page.url or apply_url
            body_text = page.inner_text("body") if page.locator("body").count() else ""
            # Expired Gupy jobs often redirect from /job/... back to the company portal root https://*.gupy.io/
            from urllib.parse import urlparse

            parsed_cur = urlparse(current_url)
            redirected_to_root = (
                "gupy.io" in (parsed_cur.netloc or "").lower()
                and parsed_cur.path.strip("/") in ("", "jobs")
                and "/job/" in (apply_url or "").lower()
            )
            if redirected_to_root or any(
                m in body_text.lower() for m in ("vaga encerrada", "inscrições encerradas", "vaga não encontrada")
            ):
                return ApplicationOutcome(
                    status="expired",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason="Vaga encerrada na Gupy.",
                    dry_run=dry_run,
                )

            # Click "Candidatar-se" if on the job overview page
            if page.locator("form input, form textarea").count() == 0:
                apply_btn = page.locator("button:has-text('Candidatar-se'), a:has-text('Candidatar-se')").first
                if apply_btn.count() > 0:
                    try:
                        apply_btn.click(timeout=5000)
                        page.wait_for_timeout(2500)
                    except Exception:  # noqa: BLE001
                        pass

            body_text = page.inner_text("body") if page.locator("body").count() else ""
            for check in (
                detect_active_captcha_on_page(page),
                detect_mfa_or_auth_wall(page.url or apply_url, body_text),
            ):
                if check:
                    return ApplicationOutcome(
                        status="pending_human_action",
                        platform=self.platform_name,
                        apply_url=page.url or apply_url,
                        reason=f"Gupy exige sessão autenticada (`python -m bot.login gupy`): {check}",
                        dry_run=dry_run,
                    )

            if page.locator("input:not([type='hidden']), textarea, select").count() == 0:
                ai_gupy = ask_ai_dom_recovery(
                    page,
                    goal="locate_form_or_apply_cta",
                    error_context="Nenhum input detectado na Gupy após tentativa padrão de Candidatar-se.",
                    job=job,
                )
                if ai_gupy:
                    if ai_gupy.get("dismiss_overlay_selector"):
                        try_dismiss_overlay(page, ai_gupy["dismiss_overlay_selector"])
                    alt_cta = resolve_locator(page, ai_gupy.get("apply_cta_selector") or "")
                    if alt_cta is not None:
                        try:
                            alt_cta.click(timeout=5000)
                            page.wait_for_timeout(2000)
                        except Exception:  # noqa: BLE001
                            pass
            if page.locator("input:not([type='hidden']), textarea, select").count() == 0:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=page.url or apply_url,
                    reason=(
                        "Gupy exige etapas proprietárias (login/testes comportamentais). "
                        "Use o link direto e o currículo personalizado gerado."
                    ),
                    dry_run=dry_run,
                )

            filler = PlaywrightFormFiller(profile)
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
