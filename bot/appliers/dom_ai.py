"""AI-assisted DOM/HTML inspection and selector recovery before human escalation."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from bot.appliers.base import (
    FormField,
    detect_active_captcha_on_page,
    detect_mfa_or_auth_wall,
)
from bot.gemini.client import agy_ask, agy_available, extract_json_object
from bot.gemini.prompts import PAGE_DOM_RECOVERY_SYSTEM, PAGE_DOM_RECOVERY_USER
from bot.profile import smart_truncate

logger = logging.getLogger("jobbot.appliers.dom_ai")

_UNSAFE_NAV_RE = re.compile(
    r"(?:\blogin\b|\blog[\s_-]?in\b|\bsign[\s_-]?in\b|\bsign[\s_-]?up\b|"
    r"\bregister\b|\bcadastro\b|\bcadastr(?:ar|o|e)\b|\bentrar\b|"
    r"\besqueci\b|\bforgot\b|\boauth\b|\bsso\b|"
    r"recaptcha|hcaptcha|turnstile|challenges\.cloudflare)",
    re.IGNORECASE,
)

PAGE_DOM_RECOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "page_status": {
            "type": "string",
            "enum": [
                "form_ready",
                "needs_click_cta",
                "multi_step",
                "expired",
                "auth_wall",
                "captcha_or_block",
                "unknown",
            ],
        },
        "diagnosis": {"type": "string"},
        "apply_cta_selector": {"type": "string"},
        "dismiss_overlay_selector": {"type": "string"},
        "iframe_selector": {"type": "string"},
        "next_button_selector": {"type": "string"},
        "submit_button_selector": {"type": "string"},
        "target_field_selector": {"type": "string"},
        "target_field_type": {"type": "string"},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "label": {"type": "string"},
                    "field_type": {"type": "string"},
                    "required": {"type": "boolean"},
                    "selector": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "placeholder": {"type": "string"},
                },
                "required": ["label", "field_type", "selector"],
            },
        },
    },
    "required": [
        "page_status",
        "diagnosis",
        "apply_cta_selector",
        "dismiss_overlay_selector",
        "iframe_selector",
        "next_button_selector",
        "submit_button_selector",
        "target_field_selector",
        "target_field_type",
        "fields",
    ],
}


def capture_page_dom_snapshot(
    page: Any,
    *,
    scope_selector: str | None = None,
    field_hint: str = "",
) -> dict[str, Any] | None:
    """Capture interactive elements and compacted HTML from a Playwright Page or Frame."""
    has_eval = hasattr(page, "evaluate")
    has_content = hasattr(page, "content")
    if not has_eval and not has_content:
        return None

    url = str(getattr(page, "url", "") or "")
    page_title = ""
    try:
        if hasattr(page, "title"):
            page_title = str(page.title() or "")
    except Exception:  # noqa: BLE001
        pass

    frames_info: list[dict[str, str]] = []
    if hasattr(page, "frames"):
        try:
            main_frame = getattr(page, "main_frame", None)
            for idx, fr in enumerate(list(page.frames)[:12]):
                if fr is main_frame:
                    continue
                frames_info.append(
                    {
                        "index": str(idx),
                        "name": str(getattr(fr, "name", "") or "")[:60],
                        "url": str(getattr(fr, "url", "") or "")[:180],
                    }
                )
        except Exception:  # noqa: BLE001
            pass

    if has_eval:
        js = r"""([scopeSel, hint]) => {
            document.querySelectorAll('[data-jobbot-ai-idx]').forEach(e => e.removeAttribute('data-jobbot-ai-idx'));
            let root = document.body || document.documentElement;
            if (scopeSel) {
                const ms = Array.from(document.querySelectorAll(scopeSel));
                const vis = ms.find(m => {
                    const s = getComputedStyle(m);
                    const r = m.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 30 && r.height > 30;
                });
                if (vis) root = vis;
                else if (ms[0]) root = ms[0];
            }
            if (!root) return null;

            const isVisible = (el) => {
                if (!el) return false;
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0 && r.width > 1 && r.height > 1;
            };

            const nearLabel = (el) => {
                if (el.id) {
                    try {
                        const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                        if (lbl && lbl.innerText) return lbl.innerText.replace(/\s+/g, ' ').trim().slice(0, 80);
                    } catch (_) {}
                }
                const pLbl = el.closest('label');
                if (pLbl && pLbl.innerText) return pLbl.innerText.replace(/\s+/g, ' ').trim().slice(0, 80);
                const grp = el.closest('[role="group"], fieldset, .form-group, .field, .mb-3, .chakra-form-control, .application-question');
                if (grp) {
                    const l = grp.querySelector('label, legend, .text, h3, h4, p');
                    if (l && l.innerText) return l.innerText.replace(/\s+/g, ' ').trim().slice(0, 80);
                }
                return '';
            };

            const rawEls = Array.from(root.querySelectorAll(
                'button, a[href], input, select, textarea, iframe, ' +
                '[role="button"], [role="combobox"], [role="listbox"], [role="textbox"], ' +
                '[role="tab"], [contenteditable="true"], [data-testid], [data-test], [onclick]'
            ));
            const elements = [];
            let idx = 0;
            for (const el of rawEls) {
                if (elements.length >= 90) break;
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (type === 'hidden') continue;
                const vis = isVisible(el);
                // Keep hidden file inputs (styled dropzones) or iframe hints, skip other invisible noise
                if (!vis && !(tag === 'input' && type === 'file') && tag !== 'iframe') continue;
                idx += 1;
                el.setAttribute('data-jobbot-ai-idx', String(idx));
                elements.push({
                    idx,
                    tag,
                    type,
                    role: (el.getAttribute('role') || '').slice(0, 30),
                    id: (el.id || '').slice(0, 50),
                    name: (el.getAttribute('name') || '').slice(0, 50),
                    text: (el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 80),
                    aria_label: (el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 80),
                    placeholder: (el.getAttribute('placeholder') || '').slice(0, 60),
                    href: (el.getAttribute('href') || '').slice(0, 100),
                    src: (el.getAttribute('src') || '').slice(0, 120),
                    data_test: (el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-automation-id') || '').slice(0, 60),
                    classes: (el.className || '').toString().replace(/\s+/g, ' ').trim().slice(0, 70),
                    disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
                    visible: vis,
                    in_form: Boolean(el.closest('form')),
                    near_label: nearLabel(el),
                });
            }

            // Pick focused HTML root (dialog / main / form / root) and strip bloat while preserving data-jobbot-ai-idx
            let htmlRoot = root;
            if (!scopeSel) {
                const candidateRoot = root.querySelector('dialog[open], [role="dialog"], form, main');
                if (candidateRoot && isVisible(candidateRoot) && (candidateRoot.innerHTML || '').length > 80) {
                    htmlRoot = candidateRoot;
                }
            }
            const clone = htmlRoot.cloneNode(true);
            clone.querySelectorAll('script, style, svg, noscript, path, link, meta').forEach(n => n.remove());
            clone.querySelectorAll('*').forEach(n => {
                for (const attr of Array.from(n.attributes)) {
                    const aname = attr.name.toLowerCase();
                    if (aname === 'style' || (attr.value || '').startsWith('data:')) {
                        n.removeAttribute(attr.name);
                    }
                }
            });
            let htmlText = (clone.outerHTML || '').replace(/\s+/g, ' ').trim();
            if (hint) {
                const lowHint = hint.toLowerCase();
                const matchEl = elements.find(e =>
                    `${e.near_label} ${e.text} ${e.name} ${e.id} ${e.placeholder}`.toLowerCase().includes(lowHint)
                );
                if (matchEl) {
                    const liveMatch = root.querySelector(`[data-jobbot-ai-idx="${matchEl.idx}"]`);
                    const blk = liveMatch ? (liveMatch.closest('form, [role="group"], fieldset, .field, div') || liveMatch) : null;
                    if (blk) {
                        const c2 = blk.cloneNode(true);
                        c2.querySelectorAll('script, style, svg').forEach(n => n.remove());
                        const snippet = (c2.outerHTML || '').replace(/\s+/g, ' ').trim().slice(0, 2200);
                        if (snippet && !htmlText.slice(0, 5500).includes(snippet.slice(0, 80))) {
                            htmlText = `<!-- MATCHED FIELD SNIPPET --> ${snippet}\n<!-- PAGE HTML --> ${htmlText}`;
                        }
                    }
                }
            }
            const shadowOverlays = [];
            for (const host of Array.from(document.querySelectorAll('#transcend-shadow-root, #usercentrics-root, #didomi-host, body > *'))) {
                if (!host.shadowRoot) continue;
                const openDlg = host.shadowRoot.querySelector('dialog[open], [role="dialog"], [data-testid="banner-container"]');
                if (openDlg) {
                    shadowOverlays.push(`#${host.id || host.tagName.toLowerCase()} (shadow overlay open)`);
                }
            }
            const baseBody = (root.innerText || document.body?.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 2300);
            const overlayNote = shadowOverlays.length ? `[BLOCKING SHADOW OVERLAYS: ${shadowOverlays.join(', ')}] ` : '';
            return {
                url: location.href || '',
                title: document.title || '',
                body_text: `${overlayNote}${baseBody}`.slice(0, 2500),
                elements,
                html_excerpt: htmlText.slice(0, 7500),
            };
        }"""
        try:
            snap = page.evaluate(js, [scope_selector or "", field_hint or ""])
            if isinstance(snap, dict):
                snap.setdefault("url", url)
                snap.setdefault("title", page_title)
                snap["frames"] = frames_info
                return snap
        except Exception as exc:  # noqa: BLE001
            logger.debug("evaluate DOM snapshot failed (%s); falling back to page.content()", exc)

    if has_content:
        try:
            raw_html = str(page.content() or "")
            cleaned = re.sub(r"<(script|style|svg|noscript)\b[^>]*>.*?</\1>", " ", raw_html, flags=re.I | re.S)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            body_txt = ""
            try:
                if hasattr(page, "inner_text"):
                    body_txt = re.sub(r"\s+", " ", page.inner_text("body") or "").strip()[:2500]
            except Exception:  # noqa: BLE001
                pass
            return {
                "url": url,
                "title": page_title,
                "body_text": body_txt,
                "elements": [],
                "frames": frames_info,
                "html_excerpt": smart_truncate(cleaned, 7500),
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("page.content DOM snapshot fallback failed: %s", exc)
    return None


def ask_ai_dom_recovery(
    page: Any,
    *,
    goal: str,
    error_context: str,
    job: dict | None = None,
    scope_selector: str | None = None,
    field: FormField | None = None,
    allow_ai: bool = True,
    max_calls_budget: list[int] | None = None,
) -> dict[str, Any] | None:
    """Send live DOM/HTML snapshot to Gemini to diagnose errors and locate elements."""
    if not allow_ai or not agy_available():
        return None
    if max_calls_budget is not None:
        if max_calls_budget[0] <= 0:
            return None
        max_calls_budget[0] -= 1

    field_hint = ""
    if field is not None:
        field_hint = f"{field.label or field.name} (type={field.field_type}, selector={field.selector}, options={field.options[:6]})"

    snap = capture_page_dom_snapshot(
        page,
        scope_selector=scope_selector,
        field_hint=(field.label or field.name or "")[:40] if field else "",
    )
    if not snap:
        return None

    # Require at least HTML or interactive elements to inspect
    if not snap.get("elements") and not snap.get("html_excerpt"):
        return None

    elements_json = json.dumps(snap.get("elements") or [], ensure_ascii=False)
    frames_json = json.dumps(snap.get("frames") or [], ensure_ascii=False)
    job = job or {}

    prompt = (
        f"{PAGE_DOM_RECOVERY_SYSTEM}\n\n"
        + PAGE_DOM_RECOVERY_USER.format(
            goal=goal,
            error_context=error_context or "Elemento/formulário não encontrado.",
            company=job.get("company") or "N/A",
            title=job.get("title") or "N/A",
            url=snap.get("url") or getattr(page, "url", "") or "",
            title_tag=snap.get("title") or "",
            field_hint=field_hint or "Nenhum",
            frames_summary=smart_truncate(frames_json, 1000),
            elements_summary=smart_truncate(elements_json, 4500),
            body_text=smart_truncate(snap.get("body_text") or "", 2000),
            html_excerpt=smart_truncate(snap.get("html_excerpt") or "", 7500),
        )
    )
    try:
        raw = agy_ask(prompt, json_schema=PAGE_DOM_RECOVERY_SCHEMA, timeout=60, max_retries=1)
        data = extract_json_object(raw)
        if not isinstance(data, dict):
            return None
        logger.info(
            "AI DOM recovery result goal=%s status=%s diagnosis=%s",
            goal,
            data.get("page_status"),
            (data.get("diagnosis") or "")[:140],
        )
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI DOM recovery failed goal=%s: %s", goal, exc)
        return None


def _locator_is_safe(loc: Any, *, selector: str) -> bool:
    """Reject selectors that point to auth/login walls or CAPTCHA widgets."""
    if _UNSAFE_NAV_RE.search(selector or ""):
        return False
    try:
        href = (loc.get_attribute("href", timeout=300) or "").strip()
        txt = (loc.inner_text(timeout=300) or "").strip()
        aria = (loc.get_attribute("aria-label", timeout=300) or "").strip()
        blob = f"{href} {txt} {aria}".strip()
        if blob and _UNSAFE_NAV_RE.search(blob):
            return False
        if href and detect_mfa_or_auth_wall(href, ""):
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def resolve_locator(
    target: Any,
    selector: str,
    *,
    scope_selector: str | None = None,
    require_visible: bool = True,
    safety_check: bool = True,
) -> Any | None:
    """Resolve a selector safely on a Page/Frame (respecting modal scope and auth/captcha guards)."""
    sel = (selector or "").strip()
    if not sel:
        return None
    try:
        if scope_selector and "[data-jobbot-ai-idx" not in sel:
            root = target.locator(scope_selector).first
            loc = root.locator(sel).first if root.count() > 0 else target.locator(sel).first
        else:
            loc = target.locator(sel).first
        if loc.count() == 0:
            loc = target.locator(sel).first
        if loc.count() == 0:
            return None
        if require_visible and hasattr(loc, "is_visible"):
            if not loc.is_visible(timeout=500):
                return None
        if safety_check and not _locator_is_safe(loc, selector=sel):
            logger.warning("Blocked unsafe AI-suggested selector: %s", sel)
            return None
        return loc
    except Exception:  # noqa: BLE001
        return None


def dismiss_blocking_overlays(page: Any, *, aggressive: bool = False) -> bool:
    """Dismiss cookie/consent overlays (including Shadow DOM banners like Transcend & Didomi).

    Only interacts with verified consent/cookie containers (never login, CAPTCHA, or form modals).
    When ``aggressive=True`` (e.g. after a pointer-event interception error), hides lingering
    consent-only backdrops that refused to close via button click.
    """
    if not hasattr(page, "evaluate"):
        return False
    js = r"""(aggressive) => {
        const UNSAFE_RE = /(login|log[\s_-]?in|sign[\s_-]?in|sign[\s_-]?up|register|cadastr|entrar|forgot|oauth|sso|recaptcha|hcaptcha|turnstile|cloudflare)/i;
        const CONSENT_HINT_RE = /(transcend|didomi|onetrust|cookiebot|usercentrics|cookie|consent|gdpr|lgpd|privacy[-_ ]?banner)/i;
        const DISMISS_BTN_RE = /^(?:reject all.*|reject|recusar.*|decline.*|close|fechar|dismiss|agree|accept.*|aceitar.*|entendi|got it|ok)$/i;

        const isVisible = (el) => {
            if (!el) return false;
            const s = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0 && r.width > 5 && r.height > 5;
        };

        let acted = false;
        const clickSafeDismissInRoot = (root) => {
            if (!root || !root.querySelectorAll) return false;
            const btns = Array.from(root.querySelectorAll('button, [role="button"], a[role="button"]'));
            const ranked = [];
            for (const b of btns) {
                if (!isVisible(b)) continue;
                const txt = (b.innerText || '').replace(/\s+/g, ' ').trim();
                const aria = (b.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
                const label = txt || aria;
                if (!label || label.length > 60) continue;
                if (UNSAFE_RE.test(label) || UNSAFE_RE.test(b.getAttribute('href') || '')) continue;
                if (!DISMISS_BTN_RE.test(label)) continue;
                // Prefer non-tracking / minimal-click actions: Reject / Close first, then Agree / Accept
                const prio = /^(?:reject|recusar|decline|close|fechar|dismiss)/i.test(label) ? 0 : 1;
                ranked.push({ b, prio });
            }
            ranked.sort((a, b) => a.prio - b.prio);
            if (ranked.length > 0) {
                try {
                    ranked[0].b.click();
                    return true;
                } catch (_) {}
            }
            return false;
        };

        // 1. Known Shadow DOM consent hosts (Transcend, Usercentrics, Didomi) + generic consent shadow hosts
        const shadowHosts = new Set(Array.from(document.querySelectorAll(
            '#transcend-shadow-root, #usercentrics-root, #didomi-host, [id*="consent" i], [id*="cookie" i]'
        )));
        for (const el of Array.from(document.querySelectorAll('body > *'))) {
            if (el.shadowRoot) {
                const meta = `${el.id || ''} ${el.className || ''}`;
                if (CONSENT_HINT_RE.test(meta)) shadowHosts.add(el);
            }
        }
        for (const host of shadowHosts) {
            if (!host || !host.shadowRoot) continue;
            if (clickSafeDismissInRoot(host.shadowRoot)) {
                acted = true;
            }
            if (aggressive) {
                const openDlgs = host.shadowRoot.querySelectorAll('dialog[open], [role="dialog"], .banner, [data-testid="banner-container"]');
                if (openDlgs.length > 0 || host.getBoundingClientRect().height > 20) {
                    openDlgs.forEach(d => {
                        try { if (typeof d.close === 'function') d.close(); } catch (_) {}
                        d.style.setProperty('display', 'none', 'important');
                        d.style.setProperty('pointer-events', 'none', 'important');
                    });
                    host.style.setProperty('pointer-events', 'none', 'important');
                    host.style.setProperty('display', 'none', 'important');
                    acted = true;
                }
            }
        }

        // 2. Light DOM consent/cookie overlays
        const lightContainers = Array.from(document.querySelectorAll(
            '#didomi-host, #didomi-popup, #onetrust-consent-sdk, #CybotCookiebotDialog, ' +
            '[id*="cookie-banner" i], [class*="cookie-banner" i], [id*="cookieConsent" i], ' +
            '[class*="cookie-consent" i], [class*="consent-banner" i], [aria-label*="cookie" i], [aria-label*="consent" i]'
        ));
        for (const box of lightContainers) {
            if (box.closest('form')) continue;
            if (isVisible(box) && clickSafeDismissInRoot(box)) {
                acted = true;
            }
            if (aggressive && isVisible(box)) {
                box.style.setProperty('pointer-events', 'none', 'important');
                box.style.setProperty('display', 'none', 'important');
                acted = true;
            }
        }
        return acted;
    }"""
    try:
        acted = bool(page.evaluate(js, bool(aggressive)))
        if acted and hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(350)
        return acted
    except Exception:  # noqa: BLE001
        return False


def try_dismiss_overlay(target: Any, selector: str, *, wait_page: Any = None) -> bool:
    """Click a cookie banner / modal close overlay identified by AI (with Shadow DOM fallback)."""
    sel = (selector or "").strip()
    if sel and _UNSAFE_NAV_RE.search(sel):
        return False
    if any(h in sel.lower() for h in ("transcend-shadow-root", "usercentrics", "didomi", ">>>")):
        if dismiss_blocking_overlays(wait_page or target, aggressive=True):
            return True
    loc = resolve_locator(target, sel, require_visible=True, safety_check=False) if sel else None
    if loc is not None:
        try:
            loc.click(timeout=2500)
            waiter = wait_page or target
            if hasattr(waiter, "wait_for_timeout"):
                waiter.wait_for_timeout(450)
            return True
        except Exception:  # noqa: BLE001
            pass
    return dismiss_blocking_overlays(wait_page or target, aggressive=True)


def resolve_iframe_target(page: Any, iframe_selector: str) -> Any | None:
    """Resolve an embedded Playwright Frame matching an AI-reported iframe selector/src."""
    sel = (iframe_selector or "").strip()
    if not sel or not hasattr(page, "frames"):
        return None
    try:
        loc = page.locator(sel).first
        if loc.count() > 0:
            src = (loc.get_attribute("src", timeout=400) or "").strip()
            name = (loc.get_attribute("name", timeout=400) or "").strip()
            for fr in list(page.frames):
                if name and getattr(fr, "name", "") == name:
                    return fr
                fr_url = getattr(fr, "url", "") or ""
                if src and src != "about:blank" and src in fr_url:
                    return fr
            # Last resort: content_frame() if Playwright ElementHandle exists
            if hasattr(loc, "element_handle"):
                eh = loc.element_handle(timeout=400)
                if eh is not None and hasattr(eh, "content_frame"):
                    cf = eh.content_frame()
                    if cf is not None:
                        return cf
    except Exception:  # noqa: BLE001
        pass
    return None


def validate_ai_fields(target: Any, raw_fields: list[dict] | None) -> list[FormField]:
    """Validate AI-discovered form field selectors against the live target DOM."""
    if not raw_fields:
        return []
    out: list[FormField] = []
    seen_selectors: set[str] = set()
    for item in raw_fields:
        if not isinstance(item, dict):
            continue
        sel = str(item.get("selector") or "").strip()
        label = str(item.get("label") or "").strip()
        ftype = str(item.get("field_type") or "text").strip().lower()
        if not sel or not label or sel in seen_selectors:
            continue
        loc = resolve_locator(
            target,
            sel,
            require_visible=(ftype != "file"),
            safety_check=True,
        )
        if loc is None:
            continue
        seen_selectors.add(sel)
        opts = [str(o).strip() for o in (item.get("options") or []) if str(o).strip()]
        out.append(
            FormField(
                name=str(item.get("name") or "").strip(),
                label=label,
                field_type=ftype,
                required=bool(item.get("required", False)),
                selector=sel,
                options=opts,
                placeholder=str(item.get("placeholder") or "").strip(),
            )
        )
    return out


def guard_before_human_escalation(page: Any) -> str | None:
    """Confirm active CAPTCHA/security blocks were not introduced during recovery."""
    return detect_active_captcha_on_page(page)
