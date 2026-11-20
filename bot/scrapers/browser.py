import json
import logging
import random
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
