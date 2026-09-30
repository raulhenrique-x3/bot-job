"""Interactive login. Run once locally (not on the VM if headless-only).

Usage:
  .venv/bin/python -m bot.login linkedin
  .venv/bin/python -m bot.login programathor
Saves storage_state to state/auth_linkedin.json / state/auth.json.
"""

import sys

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
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in TARGETS:
        print(__doc__)
        return 1
    cfg = TARGETS[sys.argv[1]]
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
            return 1
        ctx.storage_state(path=cfg["state"])
        print(f"Sessão salva em {cfg['state']}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
