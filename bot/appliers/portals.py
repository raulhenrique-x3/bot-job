"""Brazilian tech portal appliers (Programathor, GeekHunter, Nerdin)."""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

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

logger = logging.getLogger("jobbot.appliers.portals")

_NERDIN_ID_RE = re.compile(r"-(\d+)\.php", re.IGNORECASE)


class _BrazilianPortalApplier(PlatformApplier):
    """Shared safe Playwright base for Brazilian job boards."""

    domain_marker: str = ""
    expired_markers: tuple[str, ...] = (
        "vencida",
        "encerrada",
        "expirada",
        "não aceita mais",
        "não estamos aceitando novas candidaturas",
        "não está aceitando novas candidaturas",
        "candidatura indisponível",
        "vaga indisponível",
        "vaga não encontrada",
        "página não disponível",
        "página não encontrada",
        "page not found",
    )
    expired_title_markers: tuple[str, ...] = (
        "404",
        "não disponível",
        "indisponível",
        "não encontrada",
        "not found",
        "encerrada",
        "expirada",
    )

    def _is_expired_page(self, page, body_text: str | None = None) -> bool:
        try:
            title_lower = (page.title() or "").lower()
        except Exception:  # noqa: BLE001
            title_lower = ""
        if any(m in title_lower for m in self.expired_title_markers):
            return True
        if body_text is None:
            body_text = page.inner_text("body") if page.locator("body").count() else ""
        head_lower = body_text[:3000].lower()
        return any(m in head_lower for m in self.expired_markers)

    def can_handle(self, apply_url: str, job: dict) -> bool:
        return self.domain_marker in (apply_url or "").lower()

    def _prepare_form_page(self, page, apply_url: str, timeout_seconds: int) -> tuple[bool, str | None]:
        """Platform-specific hook to reach the actual application form.

        Returns (ok, pending_human_reason_if_blocked).
        """
        return True, None

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
                reason=f"Portal {self.platform_name} requer sessão de navegador para candidatura.",
                dry_run=dry_run,
            )

        from bot.scrapers.browser import goto

        with goto(browser_context, apply_url, timeout_ms=timeout_seconds * 1000) as page:
            if page is None:
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=apply_url,
                    reason=f"Falha ao abrir vaga em {self.platform_name}: {apply_url}",
                    retryable=True,
                    dry_run=dry_run,
                )

            current_url = page.url or apply_url
            body_text = page.inner_text("body") if page.locator("body").count() else ""

            if self._is_expired_page(page, body_text):
                return ApplicationOutcome(
                    status="expired",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason=f"Vaga encerrada ou indisponível em {self.platform_name}.",
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
                        reason=check,
                        dry_run=dry_run,
                    )

            ok, block_reason = self._prepare_form_page(page, current_url, timeout_seconds)
            if not ok:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=page.url or current_url,
                    reason=block_reason or f"Não foi possível abrir o formulário em {self.platform_name}.",
                    dry_run=dry_run,
                )

            if self._is_expired_page(page):
                return ApplicationOutcome(
                    status="expired",
                    platform=self.platform_name,
                    apply_url=page.url or current_url,
                    reason=f"Vaga encerrada ou indisponível em {self.platform_name}.",
                    dry_run=dry_run,
                )

            filler = PlaywrightFormFiller(profile)
            return filler.run_on_page(
                page,
                platform=self.platform_name,
                apply_url=page.url or current_url,
                job=job,
                documents=documents,
                dry_run=dry_run,
                timeout_seconds=timeout_seconds,
                cover_letter_provider=cover_letter_provider,
            )


class ProgramathorApplier(_BrazilianPortalApplier):
    platform_name = "programathor"
    domain_marker = "programathor.com.br"

    def _prepare_form_page(self, page, apply_url: str, timeout_seconds: int) -> tuple[bool, str | None]:
        # If logged out, the "Quero me candidatar" button links to /users/sign_up or /users/sign_in
        signup_btn = page.locator("a[href*='/users/sign_up']:has-text('candidatar'), a[href*='/users/sign_in']:has-text('candidatar')").first
        if signup_btn.count() > 0:
            return (
                False,
                "Programathor requer sessão autenticada (`python -m bot.login programathor` -> state/auth.json).",
            )

        # If logged in, click the apply button to reveal the modal/form
        apply_btn = page.locator(
            "button:has-text('Quero me candidatar'), a:has-text('Quero me candidatar'), "
            "button:has-text('Candidatar-se'), a:has-text('Candidatar-se')"
        ).first
        if apply_btn.count() > 0:
            try:
                apply_btn.click(timeout=5000)
                page.wait_for_timeout(1500)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Programathor apply button click note: %s", exc)
        return True, None


class GeekHunterApplier(_BrazilianPortalApplier):
    platform_name = "geekhunter"
    domain_marker = "geekhunter.com"

    def _prepare_form_page(self, page, apply_url: str, timeout_seconds: int) -> tuple[bool, str | None]:
        # GeekHunter renders its direct application form right on the job page (no login required)
        try:
            page.wait_for_selector("form input[name='email'], form input[name='name']", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        return True, None


class NerdinApplier(_BrazilianPortalApplier):
    platform_name = "nerdin"
    domain_marker = "nerdin.com.br"

    def _prepare_form_page(self, page, apply_url: str, timeout_seconds: int) -> tuple[bool, str | None]:
        # Nerdin job pages link to ../vaga_candidatura.php?id=<id> (3-step wizard, no login required)
        if "vaga_candidatura.php" not in (page.url or "").lower():
            cand_url = ""
            link = page.locator("a[href*='vaga_candidatura.php']").first
            if link.count() > 0:
                href = link.get_attribute("href") or ""
                if href:
                    cand_url = urljoin(page.url or apply_url, href)
            if not cand_url:
                m = _NERDIN_ID_RE.search(page.url or apply_url)
                if m:
                    cand_url = f"https://www.nerdin.com.br/vaga_candidatura.php?id={m.group(1)}"
            if not cand_url:
                return False, "Link de candidatura (vaga_candidatura.php) não encontrado na vaga Nerdin."

            page.goto(cand_url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
            page.wait_for_timeout(1500)

        try:
            page.wait_for_selector("input#nome_candidato, form input[type='email']", timeout=8000)
        except Exception:  # noqa: BLE001
            pass

        # Dismiss Nerdin's informational anti-scam safety notice modal if shown
        ack_btn = page.locator("#nerdin-seguranca-modal-ack, #nerdin-seguranca-modal button:has-text('Entendi')").first
        if ack_btn.count() > 0:
            try:
                if ack_btn.is_visible(timeout=1500):
                    ack_btn.click(timeout=3000)
                    page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001
                pass
        return True, None
