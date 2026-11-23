import json
import logging
import random
import signal
import threading
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("jobbot.scrapers.browser")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def launch_options() -> dict:
    return {"user_agent": UA, "locale": "pt-BR", "viewport": {"width": 1280, "height": 900}}


def load_merged_storage_state(state_dir: str = "state") -> dict | None:
    """Merge cookies and localStorage origins from all state/auth*.json session files."""
    base = Path(state_dir)
    if not base.exists():
        return None

    files = sorted(base.glob("auth*.json"))
    if not files:
        return None

    cookies_by_key: dict[tuple, dict] = {}
    origins_by_url: dict[str, dict] = {}
    loaded_names: list[str] = []

    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load session file %s: %s", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        loaded_names.append(path.name)
        for c in data.get("cookies") or []:
            if not isinstance(c, dict):
                continue
            key = (c.get("name"), c.get("domain"), c.get("path"))
            cookies_by_key[key] = c
        for orig in data.get("origins") or []:
            if not isinstance(orig, dict) or not orig.get("origin"):
                continue
            o_url = orig["origin"]
            existing = origins_by_url.setdefault(o_url, {"origin": o_url, "localStorage": []})
            ls_map = {item["name"]: item["value"] for item in existing.get("localStorage") or [] if "name" in item}
            for item in orig.get("localStorage") or []:
                if "name" in item:
                    ls_map[item["name"]] = item.get("value", "")
            existing["localStorage"] = [{"name": k, "value": v} for k, v in ls_map.items()]

    if not loaded_names:
        return None

    logger.info(
        "merged browser sessions",
        extra={"files": loaded_names, "cookies": len(cookies_by_key), "origins": len(origins_by_url)},
    )
    return {
        "cookies": list(cookies_by_key.values()),
        "origins": list(origins_by_url.values()),
    }


_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCKED_URL_FRAGMENTS = (
    "px.ads.linkedin.com",
    "doubleclick.net",
    "googletagmanager.com",
    "google-analytics.com",
    "googlesyndication.com",
    "adtrafficquality.google",
    "criteo.com",
    "crwdcntrl.net",
    "fundingchoicesmessages.google.com",
    "clarity.ms",
)


def _abort_matching_route(route, *, block_heavy: bool) -> None:
    try:
        req = route.request
        url = req.url or ""
        if any(frag in url for frag in _BLOCKED_URL_FRAGMENTS) or (
            block_heavy and req.resource_type in _BLOCKED_RESOURCE_TYPES
        ):
            route.abort()
            return
        route.fallback()
    except Exception:  # noqa: BLE001
        pass


def _abort_heavy_route(route) -> None:
    _abort_matching_route(route, block_heavy=True)


def _safe_close_page(page, timeout_s: float = 5.0) -> None:
    """Close a Playwright page with a hard wall-clock bound so mid-flight frames never hang forever."""
    use_alarm = (
        hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )
    if not use_alarm:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
        return

    def _on_alarm(signum, frame):  # noqa: ARG001
        raise TimeoutError(f"page.close() timed out after {timeout_s}s")

    prev_handler = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        page.close()
    except TimeoutError:
        logger.warning("page.close() timed out after %.1fs; abandoning hung tab", timeout_s)
    except Exception:  # noqa: BLE001
        pass
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev_handler)


@contextmanager
def goto(browser, url: str, timeout_ms: int = 45000, *, block_heavy_assets: bool = False):
    """Open a page in a throwaway context, yield it, always close."""
    try:
        page = browser.new_page()
    except Exception as exc:  # noqa: BLE001
        logger.warning("new_page failed for %s: %s", url, exc)
        yield None
        return
    try:
        page.set_default_timeout(min(timeout_ms, 15000))
        page.set_default_navigation_timeout(timeout_ms)
    except Exception:  # noqa: BLE001
        pass
    try:
        page.route("**/*", lambda route: _abort_matching_route(route, block_heavy=block_heavy_assets))
    except Exception:  # noqa: BLE001
        pass
    ok = True
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(500 if block_heavy_assets else 1500)
    except Exception as exc:  # noqa: BLE001
        logger.warning("goto failed url=%s err=%s", url, exc)
        ok = False
    try:
        yield page if ok else None
    finally:
        _safe_close_page(page, timeout_s=5.0)


def polite_sleep(low: float = 1.5, high: float = 3.0) -> None:
    time.sleep(random.uniform(low, high))
