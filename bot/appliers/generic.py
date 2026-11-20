"""Generic web form applier and email-application handler."""

from __future__ import annotations

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    PlatformApplier,
)
from bot.appliers.form_filler import PlaywrightFormFiller
from bot.profile import CandidateProfile


class EmailApplier(PlatformApplier):
    """Handles mailto: opportunities safely by preparing documents for human confirmation."""

    platform_name = "email"

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return (apply_url or "").strip().lower().startswith("mailto:")

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
        return ApplicationOutcome(
            status="pending_human_action",
            platform=self.platform_name,
            apply_url=apply_url,
            reason=(
                "Candidatura por e-mail direto detectada. Envie manualmente o currículo "
                f"personalizado gerado para {apply_url.replace('mailto:', '')}."
            ),
            dry_run=dry_run,
        )


class GenericWebApplier(PlatformApplier):
    """Fallback Playwright applier for standard HTML/ATS forms without a dedicated integration."""

    platform_name = "generic"

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return (apply_url or "").startswith(("http://", "https://"))

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
                reason="Contexto de navegador indisponível para formulário web.",
                retryable=True,
                dry_run=dry_run,
            )

        from bot.scrapers.browser import goto

        filler = PlaywrightFormFiller(profile)
        with goto(browser_context, apply_url, timeout_ms=timeout_seconds * 1000) as page:
            if page is None:
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=apply_url,
                    reason=f"Não foi possível acessar URL de candidatura: {apply_url}",
                    retryable=True,
                    dry_run=dry_run,
                )
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
