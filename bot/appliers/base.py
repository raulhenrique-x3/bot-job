"""Base contracts and safety guardrails for recruitment platform appliers."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from bot.profile import CandidateProfile

# Strict compliance & security barrier patterns — NEVER bypass any of these.
_CAPTCHA_PATTERNS = (
    re.compile(r"g-recaptcha|recaptcha/api|www\.google\.com/recaptcha", re.IGNORECASE),
    re.compile(r"h-captcha|hcaptcha\.com", re.IGNORECASE),
    re.compile(r"cf-turnstile|challenges\.cloudflare\.com/turnstile", re.IGNORECASE),
    re.compile(r"altcha|verify you are human|não sou um robô|i'm not a robot|captcha", re.IGNORECASE),
)

_MFA_AUTH_PATTERNS = (
    re.compile(r"/authwall|/checkpoint|/login|/signin|/sign_in|/sso|/(?:two-factor|2fa|mfa)(?:/|$)", re.IGNORECASE),
    re.compile(r"código de verificação|verification code|autenticação em dois fatores|faça login para se candidatar|sign in to apply|log in to apply", re.IGNORECASE),
)

_RECOMMENDATION_LETTER_PATTERNS = (
    re.compile(r"carta de recomenda[cç][aã]o|recommendation letter|letter of recommendation|reference letter|refer[êe]ncia profissional.*anex", re.IGNORECASE),
)

_SENSITIVE_LEGAL_PATTERNS = (
    re.compile(r"\b(cpf|rg|cnpj|ssn|social security|passport|passaporte|cnh|driver'?s license)\b", re.IGNORECASE),
    re.compile(r"\b(ra[cç]a|cor|etnia|ethnicity|race|gender identity|identidade de g[êe]nero|orienta[cç][aã]o sexual|sexual orientation|pcd|defici[êe]ncia|disability|veteran)\b", re.IGNORECASE),
    re.compile(r"\b(antecedentes criminais|criminal|background check|convicted|felony)\b", re.IGNORECASE),
    re.compile(r"\b(declara[cç][aã]o legal|legal declaration|under penalty of perjury|assinatura eletr[ôo]nica|electronic signature|termos legais)\b", re.IGNORECASE),
)

_COVER_LETTER_PATTERNS = (
    re.compile(
        r"cover[\s_-]*letter|carta[\s_-]*de[\s_-]*apresenta[cç][aã]o|"
        r"motivation[\s_-]*letter|carta[\s_-]*de[\s_-]*motiva[cç][aã]o",
        re.IGNORECASE,
    ),
)

_CONFIRMATION_TEXT_PATTERNS = (
    re.compile(
        r"candidatura enviada|candidatura realizada|candidatura confirmada|candidatura efetuada|"
        r"candidatura registrada|obrigado por se candidatar|recebemos sua candidatura|"
        r"inscri[cç][aã]o realizada|curr[ií]culo enviado|voc[êe] j[áa] se candidatou|"
        r"sua candidatura foi enviada|parab[ée]ns[,! ]+sua candidatura",
        re.IGNORECASE,
    ),
    re.compile(
        r"application submitted|application received|thank you for applying|thanks for applying|"
        r"we have received your application|successfully applied|your application has been sent|"
        r"you(?:'ve| have) applied|application sent|thanks for your interest",
        re.IGNORECASE,
    ),
)

_PRIVACY_TERMS_PATTERNS = (
    re.compile(
        r"(pol[ií]tica de privacidade|termos de uso|privacy policy|terms of (?:use|service)|lgpd|aceito os termos|concordo com os termos)",
        re.IGNORECASE,
    ),
)

_ACTIVE_CAPTCHA_TEXT_PATTERNS = (
    re.compile(
        r"verify you are human|n[ãa]o sou um rob[ôo]|i'?m not a robot|"
        r"selecione todas as imagens|select all images|complete the security check|"
        r"checking your browser|just a moment\.\.\.",
        re.IGNORECASE,
    ),
)

_CONFIRMATION_URL_PATTERNS = (
    re.compile(r"/(confirmation|confirmed|thank-?you|thanks|success|submitted|post-apply)", re.IGNORECASE),
)

_CONFIRMATION_ID_RE = re.compile(
    r"(?:confirmation|protocolo|protocol|reference|application\s*id|c[óo]digo(?:\s+da\s+candidatura|\s+de\s+confirma[cç][aã]o)?)"
    r"\s*(?:#|:|n[úu]mero\s*:?|number\s*:?)\s*(?=[A-Za-z0-9_-]*\d)([A-Za-z0-9_-]{4,36})",
    re.IGNORECASE,
)


class HumanInterventionRequired(Exception):
    """Raised whenever a form requires safe human intervention instead of automation."""

    def __init__(self, reason: str, category: str = "manual_review"):
        super().__init__(reason)
        self.reason = reason
        self.category = category


@dataclass
class FormField:
    name: str
    label: str
    field_type: str  # text, email, tel, url, textarea, select, radio, checkbox, file, number
    required: bool = False
    selector: str = ""
    options: list[str] = field(default_factory=list)
    placeholder: str = ""


@dataclass
class FormInspection:
    platform: str
    apply_url: str
    cover_letter_requested: bool = False
    cover_letter_required: bool = False
    blocker_reason: str = ""
    blocker_category: str = ""
    fields: list[FormField] = field(default_factory=list)
    api_metadata: dict = field(default_factory=dict)


@dataclass
class ApplicationDocuments:
    resume_pdf: str
    resume_md: str
    language: str = "pt"
    cover_letter_pdf: str = ""
    cover_letter_md: str = ""
    cover_letter_text: str = ""


@dataclass
class ApplicationOutcome:
    status: str  # applied | documents_ready (dry-run) | pending_human_action | application_failed | expired
    platform: str
    apply_url: str
    confirmation_id: str = ""
    reason: str = ""
    retryable: bool = False
    cover_letter_requested: bool = False
    filled_fields: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False


CoverLetterCallback = Callable[[], tuple[str, str]]  # returns (pdf_path, plain_text)


def detect_captcha(html_or_text: str) -> str | None:
    if not html_or_text:
        return None
    for pat in _CAPTCHA_PATTERNS:
        m = pat.search(html_or_text)
        if m:
            return f"CAPTCHA ou verificação anti-bot detectada ({m.group(0)[:40]})."
    return None


def detect_active_captcha_on_page(page) -> str | None:
    """DOM-aware CAPTCHA detector for live Playwright pages.

    Detects visible interactive CAPTCHA widgets (reCAPTCHA v2 checkbox/challenge,
    hCaptcha, Cloudflare Turnstile, Altcha) or active anti-bot gates without
    false-positiving on passive/hidden reCAPTCHA v3 <script> tags or 0x0 aframes.
    NEVER interacts with or attempts to bypass any CAPTCHA.
    """
    if not hasattr(page, "evaluate"):
        try:
            return detect_captcha(page.content())
        except Exception:  # noqa: BLE001
            return None

    js = """() => {
        const hits = [];
        // 1. Visible interactive CAPTCHA containers
        const widgets = document.querySelectorAll('.g-recaptcha, .h-captcha, .cf-turnstile, altcha-widget, [data-sitekey]');
        for (const el of widgets) {
            const size = (el.getAttribute('data-size') || '').toLowerCase();
            if (size === 'invisible') continue;
            const s = window.getComputedStyle(el);
            const r = el.getBoundingClientRect();
            if (s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0 && r.width > 40 && r.height > 30) {
                hits.push('widget:' + (el.className || el.tagName).toString().slice(0, 40));
            }
        }
        // 2. Visible CAPTCHA iframes (exclude hidden 0x0 aframes and passive bottom-right v3 badges)
        const iframes = document.querySelectorAll(
            'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], iframe[src*="challenges.cloudflare.com"]'
        );
        for (const fr of iframes) {
            const src = (fr.src || '').toLowerCase();
            if (src.includes('/aframe')) continue;
            const s = window.getComputedStyle(fr);
            const r = fr.getBoundingClientRect();
            if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') <= 0) continue;
            if (r.width < 40 || r.height < 30) continue;
            // Ignore passive reCAPTCHA v3/Enterprise corner badge (256x60 anchor with size=invisible)
            if (src.includes('/anchor') && (src.includes('size=invisible') || (r.width <= 265 && r.height <= 68 && !src.includes('size=normal')))) {
                continue;
            }
            // Ignore offscreen/hidden challenge bframe (often positioned at top:-10000px or opacity:0 on parent)
            if (src.includes('/bframe')) {
                let p = fr.parentElement;
                let hiddenAncestor = false;
                while (p && p !== document.body) {
                    const ps = window.getComputedStyle(p);
                    if (ps.visibility === 'hidden' || ps.display === 'none' || parseFloat(ps.opacity || '1') === 0) {
                        hiddenAncestor = true;
                        break;
                    }
                    p = p.parentElement;
                }
                if (hiddenAncestor || r.height < 180 || r.bottom <= 0 || r.top >= window.innerHeight) continue;
            }
            hits.push('iframe:' + src.slice(0, 70));
        }
        return hits;
    }"""
    try:
        hits = page.evaluate(js) or []
        if hits:
            return f"CAPTCHA interativo detectado ({hits[0]})."
    except Exception:  # noqa: BLE001
        pass

    try:
        body_text = page.inner_text("body")[:4000] if page.locator("body").count() else ""
    except Exception:  # noqa: BLE001
        body_text = ""

    for pat in _ACTIVE_CAPTCHA_TEXT_PATTERNS:
        m = pat.search(body_text)
        if m:
            return f"Verificação anti-bot/CAPTCHA ativa detectada ({m.group(0)})."
    return None


def is_privacy_terms_checkbox(label: str) -> bool:
    return bool(label and _PRIVACY_TERMS_PATTERNS[0].search(label))


def detect_mfa_or_auth_wall(url: str, text: str = "") -> str | None:
    path = urlparse(url or "").path or ""
    if _MFA_AUTH_PATTERNS[0].search(path):
        return f"Autenticação/login ou MFA obrigatório detectado ({url})."
    sample = (text or "")[:4000]
    m = _MFA_AUTH_PATTERNS[1].search(sample)
    if m:
        return f"Autenticação/login ou verificação MFA necessária ({m.group(0)})."
    return None


def detect_recommendation_letter_request(text: str) -> str | None:
    if not text:
        return None
    m = _RECOMMENDATION_LETTER_PATTERNS[0].search(text)
    if m:
        return (
            "Plataforma solicita carta de recomendação de terceiros "
            f"({m.group(0)}). Intervenção humana obrigatória — nunca inventar recomendações."
        )
    return None


def detect_sensitive_or_legal_field(label: str) -> str | None:
    if not label:
        return None
    for pat in _SENSITIVE_LEGAL_PATTERNS:
        m = pat.search(label)
        if m:
            return f"Campo sensível ou declaração legal requer intervenção humana: {label[:120]}"
    return None


def is_cover_letter_label(label: str) -> bool:
    return bool(label and _COVER_LETTER_PATTERNS[0].search(label))


def detect_submission_confirmation(
    url: str,
    body_text: str,
    *,
    api_confirmation_id: str = "",
) -> tuple[bool, str]:
    """Require explicit platform confirmation before marking any application as applied."""
    if api_confirmation_id:
        return True, str(api_confirmation_id)

    url_hit = bool(_CONFIRMATION_URL_PATTERNS[0].search(url or ""))
    text_sample = (body_text or "")[:6000]
    text_hit = any(p.search(text_sample) for p in _CONFIRMATION_TEXT_PATTERNS)

    if not (url_hit or text_hit):
        return False, ""

    m = _CONFIRMATION_ID_RE.search(text_sample)
    conf_id = m.group(1) if m else ""
    return True, conf_id


class PlatformApplier(ABC):
    """Extensible interface for recruitment platform appliers."""

    platform_name: str = "base"

    @abstractmethod
    def can_handle(self, apply_url: str, job: dict) -> bool:
        """Return True if this applier handles the given application URL or job source."""

    def inspect(self, job: dict, apply_url: str, browser_context=None) -> FormInspection:
        """Inspect form metadata, blockers, and whether a cover letter is requested."""
        return FormInspection(platform=self.platform_name, apply_url=apply_url)

    @abstractmethod
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
        """Fill and optionally submit an application, returning a verified outcome."""
