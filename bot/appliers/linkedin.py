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
from bot.appliers.dom_ai import ask_ai_dom_recovery, resolve_locator, try_dismiss_overlay
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

        with goto(
            browser_context,
            apply_url,
            timeout_ms=min(timeout_seconds * 1000, 35000),
            block_heavy_assets=True,
        ) as page:
            if page is None:
                return ApplicationOutcome(
                    status="application_failed",
                    platform=self.platform_name,
                    apply_url=apply_url,
                    reason=f"Não foi possível carregar vaga no LinkedIn: {apply_url}",
                    retryable=True,
                    dry_run=dry_run,
                )

            # Wait briefly for LinkedIn SDUI job header/CTA or closed banner to finish rendering
            try:
                page.wait_for_selector(
                    "button[aria-label*='candidatura simplificada' i], "
                    "button[aria-label*='easy apply' i], "
                    "button.jobs-apply-button, "
                    "button[aria-label*='candidatar-se' i], "
                    "a[aria-label*='candidatar-se' i], "
                    "a[aria-label*='apply' i], "
                    "a[href*='/safety/go/']",
                    timeout=8000,
                )
            except Exception:  # noqa: BLE001
                pass

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

            # Find Apply / Easy Apply CTA strictly in priority order (never matching job titles or description links)
            apply_btn, is_easy = self._find_apply_button(page)
            if apply_btn is None:
                ai_cta = ask_ai_dom_recovery(
                    page,
                    goal="locate_apply_cta",
                    error_context="Botão Apply / Candidatura Simplificada não encontrado no LinkedIn.",
                    job=job,
                )
                if ai_cta:
                    if ai_cta.get("dismiss_overlay_selector"):
                        try_dismiss_overlay(page, ai_cta["dismiss_overlay_selector"])
                    alt_cta = resolve_locator(page, ai_cta.get("apply_cta_selector") or "")
                    if alt_cta is not None:
                        apply_btn = alt_cta
                        try:
                            txt_blob = (
                                f"{alt_cta.inner_text(timeout=300) or ''} "
                                f"{alt_cta.get_attribute('aria-label', timeout=300) or ''}"
                            ).lower()
                            is_easy = "simplificada" in txt_blob or "easy apply" in txt_blob
                        except Exception:  # noqa: BLE001
                            is_easy = False
            if apply_btn is None:
                return ApplicationOutcome(
                    status="pending_human_action",
                    platform=self.platform_name,
                    apply_url=current_url,
                    reason="Botão de candidatura não encontrado no LinkedIn (vaga pode já estar aplicada ou exigir login).",
                    dry_run=dry_run,
                )

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
                "dialog[data-testid='dialog'], .jobs-easy-apply-modal, "
                "[data-testid='dialog-content'], [role='dialog']"
            )
            try:
                apply_btn.scroll_into_view_if_needed(timeout=3000)
                apply_btn.click(timeout=5000)
                page.wait_for_selector(modal_selector, state="visible", timeout=10000)
                # Wait for SDUI Easy Apply modal body (inputs or primary action button) to hydrate
                try:
                    page.locator(modal_selector).first.locator(
                        "input, select, textarea, "
                        "button:has-text('Avançar'), button:has-text('Avaliar'), button:has-text('Enviar'), "
                        "button:has-text('Next'), button:has-text('Review'), button:has-text('Submit')"
                    ).first.wait_for(state="visible", timeout=6000)
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(400)
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
    def _find_apply_button(page) -> tuple[object | None, bool]:
        """Locate the real LinkedIn Apply / Easy Apply CTA in strict priority order.

        Avoids comma-joined `.first` selectors that can click job titles, cards, or
        unrelated description links (e.g. 'Why Apply Through ...') appearing earlier in DOM.
        """
        js = r"""() => {
            document.querySelectorAll('[data-jobbot-apply]').forEach(e => e.removeAttribute('data-jobbot-apply'));
            const isVisible = (el) => {
                if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
                const s = window.getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') === 0) return false;
                const r = el.getBoundingClientRect();
                return r.width > 10 && r.height > 10;
            };
            const isExcluded = (el) => {
                if (el.closest('header, nav, footer, dialog, [role="dialog"]')) return true;
                if (el.closest('[componentkey^="job-card"], li[data-occludable-job-id]')) return true;
                const href = (el.getAttribute('href') || '').toLowerCase();
                if (href.includes('/jobs/search') || href.includes('/jobs/view/')) return true;
                return false;
            };
            const norm = (t) => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();

            const all = Array.from(document.querySelectorAll('button, a')).filter(el => isVisible(el) && !isExcluded(el));
            const mark = (el, easy) => {
                el.setAttribute('data-jobbot-apply', '1');
                return { found: true, easy };
            };

            // Priority 1: explicit Easy Apply aria-label or legacy class
            for (const el of all) {
                if (el.tagName !== 'BUTTON') continue;
                const aria = norm(el.getAttribute('aria-label'));
                const cls = (el.className || '').toString();
                if (aria.includes('candidatura simplificada') || aria.includes('easy apply') || cls.includes('jobs-apply-button')) {
                    const txt = norm(el.innerText);
                    const easy = aria.includes('simplificada') || aria.includes('easy apply') || txt.includes('simplificada') || txt.includes('easy apply');
                    return mark(el, easy);
                }
            }

            // Priority 2: exact visible Easy Apply button text
            for (const el of all) {
                if (el.tagName !== 'BUTTON') continue;
                const txt = norm(el.innerText);
                if (txt === 'candidatura simplificada' || txt === 'easy apply') {
                    return mark(el, true);
                }
            }

            // Priority 3: external Apply CTA (exact text or explicit apply aria-label, never description prose)
            for (const el of all) {
                const txt = norm(el.innerText);
                const aria = norm(el.getAttribute('aria-label'));
                if (txt.length > 40) continue;
                const exactCta = (
                    txt === 'candidatar-se' ||
                    txt === 'apply' ||
                    txt === 'apply now' ||
                    txt === 'candidatar-se no site da empresa' ||
                    txt === 'apply on company website'
                );
                const explicitAria = (
                    aria.startsWith('candidatar-se') ||
                    aria.startsWith('apply to ') ||
                    aria.startsWith('apply on company')
                );
                if (exactCta || explicitAria) {
                    const easy = txt.includes('simplificada') || txt.includes('easy apply');
                    return mark(el, easy);
                }
            }

            return { found: false, easy: false };
        }"""
        try:
            res = page.evaluate(js) or {}
            if res.get("found"):
                loc = page.locator("[data-jobbot-apply='1']").first
                if loc.count():
                    return loc, bool(res.get("easy"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("LinkedIn apply button discovery failed: %s", exc)
        return None, False

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
