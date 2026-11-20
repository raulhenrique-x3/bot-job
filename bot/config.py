"""Shared configuration, structured logging, secret redaction, and concurrency lock."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import dotenv

OVERALL = logging.INFO + 5

_SECRET_PATTERNS = (
    re.compile(r"(bot\d+:[A-Za-z0-9_-]{20,})"),
    re.compile(r"(Bearer\s+[A-Za-z0-9._-]{16,})", re.IGNORECASE),
    re.compile(r"((?:token|password|secret|api_key|cookie)\s*[=:]\s*)([^\s,;\"']+)", re.IGNORECASE),
)


def redact_secrets(text: str) -> str:
    """Mask tokens, bearer headers, and inline credentials before logging."""
    if not text:
        return text
    out = _SECRET_PATTERNS[0].sub("bot***REDACTED***", text)
    out = _SECRET_PATTERNS[1].sub("Bearer ***REDACTED***", out)
    out = _SECRET_PATTERNS[2].sub(r"\1***REDACTED***", out)
    return out


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True


class JsonLogFormatter(logging.Formatter):
    """Structured JSON line log formatter."""

    _RESERVED = frozenset({
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
        }
        for k, v in record.__dict__.items():
            if k not in self._RESERVED and not k.startswith("_"):
                payload[k] = redact_secrets(v) if isinstance(v, str) else v
        if record.exc_info:
            payload["exception"] = redact_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str | None = None, fmt: str | None = None) -> logging.Logger:
    log_level = (level or env("LOG_LEVEL", "INFO")).upper()
    log_format = (fmt or env("LOG_FORMAT", "json")).strip().lower()

    logger = logging.getLogger("jobbot")
    logger.setLevel(getattr(logging, log_level, logging.INFO))
    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_RedactingFilter())
    if log_format == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        )
    logger.addHandler(handler)
    return logger


def load_env(path: str | None = None) -> None:
    if path:
        dotenv.load_dotenv(path)
    else:
        dotenv.load_dotenv()


def env(key: str, default: str = "") -> str:
    val = os.environ.get(key)
    return val if val is not None and val != "" else default


def env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def env_list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key)
    if raw is None:
        raw = default
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Typed runtime settings loaded from environment variables."""

    # AI (agy CLI)
    agy_model: str = "gemini-4-neon-medium"
    agy_reasoning_effort: str = "medium"
    agy_timeout_seconds: int = 300
    agy_max_retries: int = 2

    # Matching & discovery
    job_max_age_days: int = 14
    job_match_threshold: int = 75
    max_analyses_per_run: int = 8
    default_language: str = "pt"

    # Automated applications
    auto_apply_enabled: bool = True
    application_dry_run: bool = True
    max_applications_per_run: int = 5
    max_applications_per_day: int = 20
    application_interval_seconds: int = 30
    application_max_retries: int = 3
    application_retry_backoff_minutes: int = 30
    application_timeout_seconds: int = 60

    # Paths
    resumes_dir: str = "resumes"
    applications_dir: str = "data/applications"
    database_path: str = "data/jobs.db"
    profile_json_path: str = "state/profile.json"
    profile_md_path: str = "state/profile.md"
    lock_file: str = "state/jobbot.lock"

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            agy_model=env("AGY_MODEL", "gemini-4-neon-medium"),
            agy_reasoning_effort=env("AGY_REASONING_EFFORT", "medium"),
            agy_timeout_seconds=max(30, env_int("AGY_TIMEOUT_SECONDS", 300)),
            agy_max_retries=max(0, env_int("AGY_MAX_RETRIES", 2)),
            job_max_age_days=max(1, env_int("JOB_MAX_AGE_DAYS", 14)),
            job_match_threshold=min(100, max(0, env_int("JOB_MATCH_THRESHOLD", 75))),
            max_analyses_per_run=max(1, env_int("MAX_ANALYSES_PER_RUN", 8)),
            default_language=env("DEFAULT_LANGUAGE", "pt").lower(),
            auto_apply_enabled=env_bool("AUTO_APPLY_ENABLED", True),
            application_dry_run=env_bool("APPLICATION_DRY_RUN", True),
            max_applications_per_run=max(0, env_int("MAX_APPLICATIONS_PER_RUN", 5)),
            max_applications_per_day=max(0, env_int("MAX_APPLICATIONS_PER_DAY", 20)),
            application_interval_seconds=max(0, env_int("APPLICATION_INTERVAL_SECONDS", 30)),
            application_max_retries=max(1, env_int("APPLICATION_MAX_RETRIES", 3)),
            application_retry_backoff_minutes=max(1, env_int("APPLICATION_RETRY_BACKOFF_MINUTES", 30)),
            application_timeout_seconds=max(10, env_int("APPLICATION_TIMEOUT_SECONDS", 60)),
            resumes_dir=env("RESUMES_DIR", "resumes"),
            applications_dir=env("APPLICATIONS_DIR", "data/applications"),
            database_path=env("DATABASE_PATH", "data/jobs.db"),
            profile_json_path=env("PROFILE_JSON_PATH", "state/profile.json"),
            profile_md_path=env("PROFILE_MD_PATH", "state/profile.md"),
            lock_file=env("LOCK_FILE", "state/jobbot.lock"),
            log_level=env("LOG_LEVEL", "INFO"),
            log_format=env("LOG_FORMAT", "json"),
        )


class ConcurrencyError(RuntimeError):
    """Raised when another bot-job cycle already holds the process lock."""


@contextmanager
def run_lock(lock_path: str = "state/jobbot.lock") -> Iterator[None]:
    """Exclusive non-blocking advisory file lock to prevent overlapping cycles."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConcurrencyError(f"Another bot-job instance is running (lock: {lock_path})") from exc
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
