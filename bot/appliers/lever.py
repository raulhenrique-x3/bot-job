"""Lever ATS applier — uses official Lever Postings API for inspection + Playwright form filling."""

from __future__ import annotations

import json
import logging
import re
import urllib.request

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    FormInspection,
    PlatformApplier,
)
from bot.appliers.form_filler import PlaywrightFormFiller
from bot.profile import CandidateProfile

logger = logging.getLogger("jobbot.appliers.lever")

_LEVER_URL_RE = re.compile(
    r"https?://jobs\.lever\.co/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{8,})(?:/apply)?",
    re.IGNORECASE,
)


def parse_lever_url(url: str) -> tuple[str, str] | None:
    m = _LEVER_URL_RE.search(url or "")
    if not m:
        return None
    return m.group(1), m.group(2)


class LeverApplier(PlatformApplier):
    platform_name = "lever"

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return "lever.co" in (apply_url or "").lower()

    def _fetch_posting(self, company: str, posting_id: str, timeout: int = 20) -> dict | None:
        api_url = f"https://api.lever.co/v0/postings/{company}/{posting_id}"
        req = urllib.request.Request(api_url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Lever API unavailable for %s/%s: %s", company, posting_id, exc)
            return None

    def inspect(self, job: dict, apply_url: str, browser_context=None) -> FormInspection:
        parsed = parse_lever_url(apply_url)
        target_url = apply_url.rstrip("/")
        if parsed and not target_url.endswith("/apply"):
            target_url = f"https://jobs.lever.co/{parsed[0]}/{parsed[1]}/apply"
        inspection = FormInspection(platform=self.platform_name, apply_url=target_url)
        if parsed:
            payload = self._fetch_posting(parsed[0], parsed[1])
            if payload:
                inspection.api_metadata = {
                    "company": parsed[0],
                    "posting_id": parsed[1],
                    "applyUrl": payload.get("applyUrl", target_url),
                }
                if payload.get("applyUrl"):
                    inspection.apply_url = payload["applyUrl"]
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
        target_url = inspection.apply_url
        if browser_context is None:
            return ApplicationOutcome(
                status="application_failed",
                platform=self.platform_name,
                apply_url=target_url,
                reason="Contexto de navegador indisponível para formulário Lever.",
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
                    reason=f"Não foi possível carregar página Lever: {target_url}",
                    retryable=True,
                    dry_run=dry_run,
                )
            return filler.run_on_page(
                page,
                platform=self.platform_name,
                apply_url=target_url,
                job=job,
                documents=documents,
                dry_run=dry_run,
                timeout_seconds=timeout_seconds,
                cover_letter_provider=cover_letter_provider,
            )
