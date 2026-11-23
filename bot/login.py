"""Interactive and headless-friendly login for job portals (including Jobicy via Google OAuth).

Usage:
  .venv/bin/python -m bot.login linkedin
  .venv/bin/python -m bot.login programathor
  .venv/bin/python -m bot.login geekhunter
  .venv/bin/python -m bot.login gupy
  .venv/bin/python -m bot.login wellfound
  .venv/bin/python -m bot.login avanade
  .venv/bin/python -m bot.login jobicy
  .venv/bin/python -m bot.login jobicy --import-cookies
  .venv/bin/python -m bot.login jobicy --check
Saves storage_state to state/auth_*.json with restricted 0600 permissions.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode, urlparse

from playwright.sync_api import sync_playwright

from bot.scrapers.browser import launch_options

JOBICY_GOOGLE_CLIENT_ID = (
    "517266074729-30jjo2la7nqalj4gkgogsb9nle71ffim.apps.googleusercontent.com"
)
JOBICY_GOOGLE_REDIRECT_URI = "https://jobicy.com/api/v2/google-auth.php"


def build_jobicy_google_oauth_url(redirect_to: str = "https://jobicy.com/dashboard-page") -> str:
    """Build Jobicy's official Google OAuth2 sign-in URL (candidate role)."""
    state = json.dumps({"role": "candidate", "redirect_to": redirect_to}, separators=(",", ":"))
    params = urlencode(
        {
            "response_type": "code",
            "client_id": JOBICY_GOOGLE_CLIENT_ID,
            "redirect_uri": JOBICY_GOOGLE_REDIRECT_URI,
            "scope": "openid email profile",
            "access_type": "online",
            "prompt": "select_account",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{params}"


JOBICY_GOOGLE_OAUTH_URL = build_jobicy_google_oauth_url()

TARGETS: dict[str, dict] = {
    "linkedin": {
        "url": "https://www.linkedin.com/login",
        "state": "state/auth_linkedin.json",
        "check_url": "https://www.linkedin.com/jobs/search/?keywords=desenvolvedor&location=Brasil",
        "auth_fail": ("/authwall", "/login", "/checkpoint"),
    },
    "programathor": {
        "url": "https://programathor.com.br/users/sign_in",
        "state": "state/auth.json",
        "check_url": "https://programathor.com.br/jobs",
        "auth_fail": ("/users/sign_in",),
    },
    "geekhunter": {
        "url": "https://www.geekhunter.com.br/signin",
        "state": "state/auth_geekhunter.json",
        "check_url": "https://www.geekhunter.com/pt/vagas",
        "auth_fail": ("/signin", "/login"),
    },
    "gupy": {
        "url": "https://portal.gupy.io/",
        "state": "state/auth_gupy.json",
        "check_url": "https://portal.gupy.io/",
        "auth_fail": ("/login", "/signin"),
    },
    "wellfound": {
        "url": "https://wellfound.com/login",
        "state": "state/auth_wellfound.json",
        "check_url": "https://wellfound.com/jobs",
        "auth_fail": ("/login", "/signup", "/onboarding"),
    },
    "avanade": {
        "url": "https://accenture.wd103.myworkdayjobs.com/en-US/AvanadeCareers/login",
        "state": "state/auth_avanade.json",
        "check_url": "https://accenture.wd103.myworkdayjobs.com/en-US/AvanadeCareers/userHome",
        "auth_fail": ("/login",),
    },
    "jobicy": {
        "url": "https://jobicy.com/dashboard-page",
        "google_oauth_url": JOBICY_GOOGLE_OAUTH_URL,
        "state": "state/auth_jobicy.json",
        "check_url": "https://jobicy.com/dashboard-page",
        "auth_fail": ("/login", "/signin", "accounts.google.com"),
        "auth_fail_selectors": (".google-login-button", "#btn_check_email"),
        "required_cookie_prefixes": ("wordpress_logged_in_",),
        "default_domain": ".jobicy.com",
        "auto_google": True,
    },
}

_SAME_SITE_MAP = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": "Lax",
}

_CURL_COOKIE_RE = re.compile(
    r"""(?:-H\s+['"]cookie\s*:\s*([^'"]+)['"]|-b\s+['"]([^'"]+)['"]|--cookie\s+['"]([^'"]+)['"])""",
    re.IGNORECASE,
)


def _normalize_cookie(item: dict, default_domain: str) -> dict | None:
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    value = str(item.get("value") or "")
    domain = str(item.get("domain") or default_domain).strip() or default_domain
    path = str(item.get("path") or "/").strip() or "/"
    raw_exp = item.get("expires")
    if raw_exp is None:
        raw_exp = item.get("expirationDate")
    try:
        expires = float(raw_exp) if raw_exp not in (None, "") else -1.0
    except (TypeError, ValueError):
        expires = -1.0
    raw_ss = str(item.get("sameSite") or "Lax").strip().lower()
    same_site = _SAME_SITE_MAP.get(raw_ss, "Lax")
    secure = bool(item.get("secure", True))
    if same_site == "None":
        secure = True
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "expires": expires,
        "httpOnly": bool(item.get("httpOnly", False)),
        "secure": secure,
        "sameSite": same_site,
    }


def parse_imported_cookies(raw: str, default_domain: str) -> list[dict]:
    """Parse Playwright storage_state JSON, Cookie-Editor JSON, cURL snippet, or Cookie header."""
    text = (raw or "").strip()
    if not text:
        return []

    # 1. JSON (Playwright storage_state dict or Cookie-Editor list)
    if text.startswith(("{", "[")):
        try:
            data = json.loads(text)
            items = data.get("cookies", []) if isinstance(data, dict) else data
            if isinstance(items, list):
                out = []
                for entry in items:
                    if isinstance(entry, dict):
                        norm = _normalize_cookie(entry, default_domain)
                        if norm:
                            out.append(norm)
                return out
        except json.JSONDecodeError:
            pass

    # 2. Full cURL command copied from DevTools
    cookie_header = text
    m_curl = _CURL_COOKIE_RE.search(text)
    if m_curl:
        cookie_header = next((g for g in m_curl.groups() if g), "")
    elif cookie_header.lower().startswith("cookie:"):
        cookie_header = cookie_header.split(":", 1)[1]

    # 3. Raw "name=val; name2=val2" header string
    out: list[dict] = []
    for part in cookie_header.replace("\n", ";").split(";"):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if not name or name.lower() in ("path", "domain", "expires", "max-age", "samesite", "secure", "httponly"):
            continue
        norm = _normalize_cookie({"name": name, "value": value.strip()}, default_domain)
        if norm:
            out.append(norm)
    return out


def is_session_authenticated(page, cookies: list[dict], cfg: dict) -> tuple[bool, str]:
    """Check URL, login-wall selectors, and required cookies for an authenticated portal session."""
    url = page.url or ""
    if any(marker in url for marker in cfg.get("auth_fail", ())):
        return False, f"ainda em URL de autenticação ({url})"

    for sel in cfg.get("auth_fail_selectors", ()):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1000):
                return False, f"tela de login ainda visível ({sel})"
        except Exception:  # noqa: BLE001
            continue

    req_prefixes = cfg.get("required_cookie_prefixes") or ()
    if req_prefixes:
        cookie_names = [str(c.get("name") or "") for c in cookies]
        if not any(name.startswith(pref) for name in cookie_names for pref in req_prefixes):
            return False, f"cookie de sessão esperado não encontrado ({', '.join(req_prefixes)})"

    return True, "sessão autenticada"


def _save_storage_state(state_path: Path, state_data: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state_data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(state_path, 0o600)


def verify_storage_state(target_name: str, storage_state: dict) -> tuple[bool, str]:
    """Validate a Playwright storage_state dict live against target's check_url in headless mode."""
    cfg = TARGETS[target_name]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx_opts = dict(launch_options())
            ctx_opts["storage_state"] = storage_state
            ctx = browser.new_context(**ctx_opts)
            page = ctx.new_page()
            page.goto(cfg["check_url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            cookies = ctx.cookies()
            ok, reason = is_session_authenticated(page, cookies, cfg)
            return ok, reason
        finally:
            browser.close()


def _launch_interactive_browser(pw, *, prefer_chrome: bool = False):
    """Launch a visible browser; prefer Chrome + disable AutomationControlled for Google OAuth."""
    args = ["--disable-blink-features=AutomationControlled"]
    if prefer_chrome:
        try:
            return pw.chromium.launch(headless=False, channel="chrome", args=args)
        except Exception:  # noqa: BLE001
            pass
    return pw.chromium.launch(headless=False, args=args)


def _has_gui_display() -> bool:
    if sys.platform != "linux":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _run_import_cookies(target_name: str, cfg: dict, *, cookies_file: str = "", verify: bool = True) -> int:
    state_path = Path(cfg["state"])
    default_domain = cfg.get("default_domain") or f".{urlparse(cfg['url']).netloc}"

    if cookies_file:
        raw = Path(cookies_file).read_text(encoding="utf-8")
    else:
        oauth_url = cfg.get("google_oauth_url") or cfg["url"]
        print(f"\n=== Autenticação {target_name} via Google (modo headless/VPS) ===")
        print("1. Abra o link oficial abaixo no seu navegador normal e faça login com sua conta Google:")
        print(f"\n   {oauth_url}\n")
        print("2. Após entrar no Jobicy (https://jobicy.com/dashboard-page), copie os cookies da aba")
        print("   (via extensão Cookie-Editor -> Export JSON, ou DevTools -> Network -> 'Copy as cURL' / header Cookie).")
        print("3. Cole o conteúdo abaixo e pressione ENTER + Ctrl-D (EOF):\n")
        raw = sys.stdin.read()

    cookies = parse_imported_cookies(raw, default_domain=default_domain)
    if not cookies:
        print("Erro: nenhum cookie válido encontrado na entrada fornecida.")
        return 1

    storage_state = {"cookies": cookies, "origins": []}
    if verify:
        print(f"Validando sessão {target_name} em {cfg['check_url']}...")
        ok, reason = verify_storage_state(target_name, storage_state)
        if not ok:
            print(f"Validação falhou: {reason}")
            return 1
        print(f"Validação OK: {reason}")

    _save_storage_state(state_path, storage_state)
    print(f"Sessão salva em {state_path} (chmod 0600, {len(cookies)} cookies)")
    return 0


def _run_interactive_login(target_name: str, cfg: dict, *, use_google: bool = False) -> int:
    state_path = Path(cfg["state"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    start_url = cfg["google_oauth_url"] if (use_google and cfg.get("google_oauth_url")) else cfg["url"]
    prefer_chrome = bool(use_google or cfg.get("auto_google"))

    with sync_playwright() as pw:
        browser = _launch_interactive_browser(pw, prefer_chrome=prefer_chrome)
        ctx = browser.new_context(**launch_options())
        page = ctx.new_page()
        page.goto(start_url, wait_until="domcontentloaded")
        if use_google and cfg.get("google_oauth_url"):
            print("Janela aberta no login Google do Jobicy.")
        input("Faça login no navegador e pressione ENTER aqui quando terminar...")
        page.goto(cfg["check_url"], wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        ok, reason = is_session_authenticated(page, ctx.cookies(), cfg)
        if not ok:
            print(f"Login falhou: {reason}")
            browser.close()
            return 1
        ctx.storage_state(path=str(state_path))
        os.chmod(state_path, 0o600)
        print(f"Sessão salva em {state_path} (chmod 0600)")
        browser.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bot.login",
        description="Autentica sessões de portais de vagas e salva state/auth_*.json (chmod 0600).",
    )
    parser.add_argument("target", nargs="?", choices=sorted(TARGETS.keys()), help="Portal alvo")
    parser.add_argument(
        "--google",
        action="store_true",
        help="Abrir diretamente o fluxo OAuth do Google (padrão no Jobicy)",
    )
    parser.add_argument(
        "--import-cookies",
        action="store_true",
        help="Importar cookies autenticados (modo headless/VPS sem navegador gráfico)",
    )
    parser.add_argument(
        "--cookies-file",
        default="",
        help="Arquivo contendo cookies (JSON ou header Cookie) para importar junto com --import-cookies",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Pular validação online ao importar cookies",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verificar se a sessão salva em state/auth_*.json ainda está válida",
    )
    parser.add_argument(
        "--print-oauth-url",
        action="store_true",
        help="Exibir a URL oficial de login Google OAuth do portal e sair",
    )

    args = parser.parse_args(argv)
    if not args.target:
        print(__doc__)
        return 1

    cfg = TARGETS[args.target]
    state_path = Path(cfg["state"])

    if args.print_oauth_url:
        print(cfg.get("google_oauth_url") or cfg["url"])
        return 0

    if args.check:
        if not state_path.exists():
            print(f"Sessão não encontrada em {state_path}")
            return 1
        data = json.loads(state_path.read_text(encoding="utf-8"))
        ok, reason = verify_storage_state(args.target, data)
        print(f"{args.target}: {'OK' if ok else 'INVÁLIDA'} ({reason})")
        return 0 if ok else 1

    if args.import_cookies or args.cookies_file:
        return _run_import_cookies(args.target, cfg, cookies_file=args.cookies_file, verify=not args.no_verify)

    if not _has_gui_display():
        print(
            f"Ambiente sem interface gráfica ($DISPLAY ausente) — usando fluxo de importação para {args.target}."
        )
        return _run_import_cookies(args.target, cfg, verify=not args.no_verify)

    use_google = bool(args.google or cfg.get("auto_google"))
    return _run_interactive_login(args.target, cfg, use_google=use_google)


if __name__ == "__main__":
    sys.exit(main())
