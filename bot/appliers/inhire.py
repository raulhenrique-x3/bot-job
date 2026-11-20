"""InHire ATS applier (widely used by Brazilian tech companies via *.inhire.app)."""

from __future__ import annotations

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    PlatformApplier,
    detect_active_captcha_on_page,
    detect_mfa_or_auth_wall,
)
from bot.appliers.form_filler import PlaywrightFormFiller
from bot.profile import CandidateProfile


class InHireApplier(PlatformApplier):
    platform_name = "inhire"

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return "inhire.app" in (apply_url or "").lower()

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

            # Wait for SPA hydration
            try:
                page.wait_for_selector(
                    "button:has-text('Candidatar'), a:has-text('Candidatar'), input[type='email'], input[type='file']",
                    timeout=10000,
                )
            except Exception:  # noqa: BLE001
                page.wait_for_timeout(2000)

            body_text = page.inner_text("body") if page.locator("body").count() else ""
            if any(
                m in body_text.lower()
                for m in (
                    "vaga encerrada",
                    "vaga não encontrada",
                    "position closed",
                    "não é válido",
                    "nao e valido",
                    "não está mais disponível",
                )
            ):
                return ApplicationOutcome(
                    status="expired",
                    platform=self.platform_name,
                    apply_url=page.url or apply_url,
                    reason="Vaga encerrada ou indisponível no InHire.",
                    dry_run=dry_run,
                )

            for check in (
                detect_active_captcha_on_page(page),
                detect_mfa_or_auth_wall(page.url or apply_url, body_text),
            ):
                if check:
                    return ApplicationOutcome(
                        status="pending_human_action",
                        platform=self.platform_name,
                        apply_url=page.url or apply_url,
                        reason=check,
                        dry_run=dry_run,
                    )

            # Click "Candidatar-se" button if on the description view
            if page.locator("input[type='email'], input[type='file']").count() == 0:
                apply_btn = page.locator(
                    "button:has-text('Candidatar'), a:has-text('Candidatar'), button:has-text('Apply')"
                ).first
                if apply_btn.count() > 0:
                    try:
                        apply_btn.click(timeout=5000)
                        page.wait_for_timeout(2000)
                    except Exception:  # noqa: BLE001
                        pass

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
