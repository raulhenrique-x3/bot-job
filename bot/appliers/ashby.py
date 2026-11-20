"""Ashby HQ ATS applier."""

from __future__ import annotations

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    PlatformApplier,
)
from bot.appliers.form_filler import PlaywrightFormFiller
from bot.profile import CandidateProfile


class AshbyApplier(PlatformApplier):
    platform_name = "ashby"

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return "ashbyhq.com" in (apply_url or "").lower()

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
        target_url = apply_url.rstrip("/")
        if "/application" not in target_url:
            target_url = f"{target_url}/application"

        if browser_context is None:
            return ApplicationOutcome(
                status="application_failed",
                platform=self.platform_name,
                apply_url=target_url,
                reason="Contexto de navegador indisponível para formulário Ashby.",
                retryable=True,
                dry_run=dry_run,
            )

        from bot.scrapers.browser import goto

        filler = PlaywrightFormFiller(profile)
        with goto(browser_context, target_url, timeout_ms=timeout_seconds * 1000) as page:
            if page is None:
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=target_url,
                    reason=f"Não foi possível carregar página Ashby: {target_url}",
                    retryable=True,
                    dry_run=dry_run,
                )
            try:
                page.wait_for_selector("input#_systemfield_name, input[type='email'], input[type='file']", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            return filler.run_on_page(
                page,
                platform=self.platform_name,
                apply_url=page.url or target_url,
                job=job,
                documents=documents,
                dry_run=dry_run,
                timeout_seconds=timeout_seconds,
                cover_letter_provider=cover_letter_provider,
            )
