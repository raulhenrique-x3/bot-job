"""Extensible registry that resolves target platforms and dispatches applications."""

from __future__ import annotations

import logging

from bot.appliers.ashby import AshbyApplier
from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    PlatformApplier,
)
from bot.appliers.detector import detect_platform, resolve_apply_url
from bot.appliers.generic import EmailApplier, GenericWebApplier
from bot.appliers.greenhouse import GreenhouseApplier
from bot.appliers.gupy import GupyApplier
from bot.appliers.inhire import InHireApplier
from bot.appliers.lever import LeverApplier
from bot.appliers.linkedin import LinkedInApplier
from bot.appliers.portals import GeekHunterApplier, NerdinApplier, ProgramathorApplier
from bot.profile import CandidateProfile

logger = logging.getLogger("jobbot.appliers.registry")


class ApplierRegistry:
    """Extensible registry of platform-specific appliers with generic fallback."""

    def __init__(self, appliers: list[PlatformApplier] | None = None):
        if appliers is not None:
            self._appliers: list[PlatformApplier] = list(appliers)
        else:
            self._appliers = [
                GreenhouseApplier(),
                LeverApplier(),
                AshbyApplier(),
                InHireApplier(),
                GupyApplier(),
                LinkedInApplier(external_dispatcher=self.dispatch),
                ProgramathorApplier(),
                GeekHunterApplier(),
                NerdinApplier(),
                EmailApplier(),
                GenericWebApplier(),
            ]

    def register(self, applier: PlatformApplier, *, priority: bool = True) -> None:
        """Register an additional platform applier without modifying existing modules."""
        if priority:
            self._appliers.insert(0, applier)
        else:
            self._appliers.append(applier)

    def select(self, apply_url: str, job: dict) -> PlatformApplier | None:
        for applier in self._appliers:
            if applier.can_handle(apply_url, job):
                return applier
        return None

    def dispatch(
        self,
        job: dict,
        profile: CandidateProfile,
        documents: ApplicationDocuments,
        *,
        browser_context=None,
        dry_run: bool = True,
        timeout_seconds: int = 60,
        cover_letter_provider: CoverLetterCallback | None = None,
    ) -> ApplicationOutcome:
        apply_url, detected = resolve_apply_url(
            job,
            browser_context=browser_context,
            timeout_ms=min(timeout_seconds * 1000, 25000),
        )
        if not apply_url:
            return ApplicationOutcome(
                status="pending_human_action",
                platform=detected or "unknown",
                apply_url=job.get("url") or "",
                reason="Link de candidatura não encontrado na vaga.",
                dry_run=dry_run,
            )

        applier = self.select(apply_url, job)
        if applier is None:
            return ApplicationOutcome(
                status="pending_human_action",
                platform=detect_platform(apply_url, job.get("source", "")),
                apply_url=apply_url,
                reason=f"Nenhuma integração compatível para a URL: {apply_url}",
                dry_run=dry_run,
            )

        logger.info(
            "dispatching application",
            extra={
                "job_id": job.get("id"),
                "platform": applier.platform_name,
                "apply_url": apply_url,
                "dry_run": dry_run,
            },
        )
        try:
            outcome = applier.apply(
                job,
                apply_url,
                profile,
                documents,
                browser_context=browser_context,
                dry_run=dry_run,
                timeout_seconds=timeout_seconds,
                cover_letter_provider=cover_letter_provider,
            )
            if not outcome.platform:
                outcome.platform = applier.platform_name
            return outcome
        except Exception as exc:  # noqa: BLE001
            logger.exception("applier crashed job=%s platform=%s", job.get("id"), applier.platform_name)
            return ApplicationOutcome(
                status="application_failed",
                platform=applier.platform_name,
                apply_url=apply_url,
                reason=f"Erro inesperado na plataforma {applier.platform_name}: {exc}",
                retryable=True,
                dry_run=dry_run,
            )
