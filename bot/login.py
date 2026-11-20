"""Interactive login. Run once locally (not on a headless-only VM).

Usage:
  .venv/bin/python -m bot.login linkedin
  .venv/bin/python -m bot.login programathor
  .venv/bin/python -m bot.login geekhunter
  .venv/bin/python -m bot.login gupy
Saves storage_state to state/auth_*.json with restricted 0600 permissions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from bot.scrapers.browser import launch_options

TARGETS = {
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
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in TARGETS:
        print(__doc__)
        return 1
    cfg = TARGETS[sys.argv[1]]
    state_path = Path(cfg["state"])
    state_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(**launch_options())
        page = ctx.new_page()
        page.goto(cfg["url"], wait_until="domcontentloaded")
        input("Faça login no navegador e pressione ENTER aqui quando terminar...")
        page.goto(cfg["check_url"], wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        if any(marker in page.url for marker in cfg["auth_fail"]):
            print(f"Login falhou: ainda em {page.url}")
            browser.close()
            return 1
        ctx.storage_state(path=str(state_path))
        os.chmod(state_path, 0o600)
        print(f"Sessão salva em {state_path} (chmod 0600)")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
