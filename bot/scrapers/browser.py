import logging
import random
import time
from contextlib import contextmanager

logger = logging.getLogger("jobbot.scrapers.browser")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def launch_options() -> dict:
    return {"user_agent": UA, "locale": "pt-BR", "viewport": {"width": 1280, "height": 900}}


@contextmanager
def goto(browser, url: str, timeout_ms: int = 45000):
    """Open a page in a throwaway context, yield it, always close."""
    try:
        page = browser.new_page()
    except Exception as exc:  # noqa: BLE001
        logger.warning("new_page failed for %s: %s", url, exc)
        yield None
        return
    ok = True
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1500)
    except Exception as exc:  # noqa: BLE001
        logger.warning("goto failed url=%s err=%s", url, exc)
        ok = False
    try:
        yield page if ok else None
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


def polite_sleep(low: float = 1.5, high: float = 3.0) -> None:
    time.sleep(random.uniform(low, high))
