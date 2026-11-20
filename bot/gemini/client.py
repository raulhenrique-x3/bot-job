"""Unified agy CLI client configured for Gemini 4 Neon (Medium) by default."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time

from bot.config import env, env_int

logger = logging.getLogger("jobbot.gemini.client")

DEFAULT_MODEL = "gemini-4-neon-medium"
DEFAULT_EFFORT = "medium"
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def agy_available() -> bool:
    return shutil.which("agy") is not None


def resolve_model_and_effort(
    model: str | None = None,
    effort: str | None = None,
) -> tuple[str, str]:
    """Resolve model slug and reasoning effort, defaulting to Gemini 4 Neon (Medium)."""
    raw_model = (model or env("AGY_MODEL", DEFAULT_MODEL)).strip()
    raw_effort = (effort or env("AGY_REASONING_EFFORT", DEFAULT_EFFORT)).strip().lower()
    if raw_effort not in VALID_EFFORTS:
        raw_effort = DEFAULT_EFFORT

    # Normalize human-friendly names like "Gemini 4 Neon" or "gemini-4-neon"
    normalized = re.sub(r"[\s_()]+", "-", raw_model.lower()).strip("-")
    if normalized in ("gemini-4-neon", "gemini4-neon"):
        resolved_model = f"gemini-4-neon-{raw_effort}"
    else:
        resolved_model = raw_model or DEFAULT_MODEL
    return resolved_model, raw_effort


def agy_ask(
    prompt: str,
    *,
    json_schema: dict | str | None = None,
    model: str | None = None,
    effort: str | None = None,
    timeout: int | None = None,
    max_retries: int | None = None,
) -> str:
    """Invoke the local agy CLI with Gemini 4 Neon (Medium) defaults and controlled retries."""
    binary = shutil.which("agy")
    if not binary:
        raise RuntimeError("agy not found in PATH (~/.local/bin/agy)")

    resolved_model, resolved_effort = resolve_model_and_effort(model, effort)
    timeout_s = timeout if timeout is not None else env_int("AGY_TIMEOUT_SECONDS", 300)
    retries = max_retries if max_retries is not None else env_int("AGY_MAX_RETRIES", 2)

    cmd = [binary, "-p", prompt, "--model", resolved_model, "--effort", resolved_effort]
    if json_schema is not None:
        schema_str = json.dumps(json_schema) if isinstance(json_schema, dict) else json_schema
        cmd.extend(["--output-format", "json", "--json-schema", schema_str])
    else:
        cmd.extend(["--output-format", "text"])

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
            if resp.returncode != 0:
                err = resp.stderr.strip() or f"agy exit {resp.returncode}"
                raise RuntimeError(err)
            if json_schema is None:
                return resp.stdout.strip()
            try:
                wrapper = json.loads(resp.stdout)
            except json.JSONDecodeError:
                return resp.stdout.strip()
            usage = wrapper.get("usage") or {}
            logger.info(
                "agy completed",
                extra={
                    "model": resolved_model,
                    "effort": resolved_effort,
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "thinking_tokens": usage.get("thinking_tokens"),
                    "duration_s": wrapper.get("duration_seconds"),
                },
            )
            return (wrapper.get("response") or "").strip()
        except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
            last_exc = exc
            if attempt < retries:
                backoff = 2 ** attempt
                logger.warning(
                    "agy call attempt %d/%d failed (%s); retrying in %ds",
                    attempt + 1,
                    retries + 1,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
    assert last_exc is not None
    raise last_exc


def extract_json_object(raw: str) -> dict | None:
    """Safely extract the first JSON object from an agy response string."""
    if not raw:
        return None
    stripped = raw.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", stripped, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None
