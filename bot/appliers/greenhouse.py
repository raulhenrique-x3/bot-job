"""Greenhouse ATS applier — prioritizes official Job Board API inspection + Playwright submission."""

from __future__ import annotations

import json
import logging
import re
import urllib.request

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    CoverLetterCallback,
    FormField,
    FormInspection,
    PlatformApplier,
    detect_recommendation_letter_request,
    is_cover_letter_label,
)
from bot.appliers.form_filler import PlaywrightFormFiller
from bot.profile import CandidateProfile

logger = logging.getLogger("jobbot.appliers.greenhouse")

_GH_URL_RE = re.compile(
    r"https?://(?:job-)?boards\.greenhouse\.io/(?:embed/job_app\?.*)?([A-Za-z0-9_-]+)/jobs/(\d+)",
    re.IGNORECASE,
)


def parse_greenhouse_url(url: str) -> tuple[str, str] | None:
    m = _GH_URL_RE.search(url or "")
    if not m:
        return None
    return m.group(1), m.group(2)


class GreenhouseApplier(PlatformApplier):
    platform_name = "greenhouse"

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return "greenhouse.io" in (apply_url or "").lower()

    def _fetch_board_api(self, board_token: str, job_id: str, timeout: int = 20) -> dict | None:
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}?questions=true"
        req = urllib.request.Request(api_url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Greenhouse API unavailable for %s/%s: %s", board_token, job_id, exc)
            return None

    def inspect(self, job: dict, apply_url: str, browser_context=None) -> FormInspection:
        parsed = parse_greenhouse_url(apply_url)
        inspection = FormInspection(platform=self.platform_name, apply_url=apply_url)
        if not parsed:
            return inspection
        board_token, gh_job_id = parsed
        payload = self._fetch_board_api(board_token, gh_job_id)
        if not payload:
            return inspection

        inspection.api_metadata = {
            "board_token": board_token,
            "greenhouse_job_id": gh_job_id,
            "title": payload.get("title", ""),
        }
        for q in payload.get("questions") or []:
            label = str(q.get("label") or "").strip()
            required = bool(q.get("required"))
            rec_err = detect_recommendation_letter_request(label)
            if rec_err:
                inspection.blocker_reason = rec_err
                inspection.blocker_category = "recommendation_letter"
            if is_cover_letter_label(label):
                inspection.cover_letter_requested = True
                if required:
                    inspection.cover_letter_required = True
            for f in q.get("fields") or []:
                options = [str(v.get("label") or "") for v in (f.get("values") or []) if isinstance(v, dict)]
                inspection.fields.append(
                    FormField(
                        name=str(f.get("name") or ""),
                        label=label,
                        field_type=str(f.get("type") or "text"),
                        required=required,
                        options=options,
                    )
                )
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
        if inspection.blocker_reason:
            return ApplicationOutcome(
                status="pending_human_action",
                platform=self.platform_name,
                apply_url=apply_url,
                reason=inspection.blocker_reason,
                dry_run=dry_run,
            )

        if browser_context is None:
            return ApplicationOutcome(
                status="application_failed",
                platform=self.platform_name,
                apply_url=apply_url,
                reason="Contexto de navegador indisponível para formulário Greenhouse.",
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
                    reason=f"Não foi possível carregar página Greenhouse: {apply_url}",
                    retryable=True,
                    dry_run=dry_run,
                )
            current_url = page.url or apply_url
            body_text = page.inner_text("body")[:4000].lower() if page.locator("body").count() else ""
            if (
                "error=true" in current_url.lower()
                or "não está mais aberta" in body_text
                or "no longer open" in body_text
                or "position has been filled" in body_text
                or "job not found" in body_text
            ):
                return ApplicationOutcome(
                    status="expired",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason="Vaga encerrada no Greenhouse.",
                    dry_run=dry_run,
                )

            # Some Greenhouse links redirect to a company career listing with an "Apply now" link
            if page.locator("input[type='file'], input[name*='first_name'], input#first_name").count() == 0:
                apply_link = page.locator(
                    "a:has-text('Apply now'), a:has-text('Apply for this'), "
                    "button:has-text('Apply now'), button:has-text('Apply for this')"
                ).first
                if apply_link.count() > 0:
                    try:
                        apply_link.click(timeout=5000)
                        page.wait_for_timeout(2000)
                    except Exception:  # noqa: BLE001
                        pass
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
