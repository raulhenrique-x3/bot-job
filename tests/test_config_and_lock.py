"""Tests for Settings, structured logging secret redaction, agy model/effort resolution, and run_lock."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from bot.config import ConcurrencyError, JsonLogFormatter, Settings, redact_secrets, run_lock
from bot.gemini.client import resolve_model_and_effort


def test_settings_defaults_gemini_4_neon_medium(monkeypatch):
    for key in ("AGY_MODEL", "AGY_REASONING_EFFORT", "APPLICATION_DRY_RUN", "AUTO_APPLY_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    s = Settings.from_env()
    assert s.agy_model == "gemini-4-neon-medium"
    assert s.agy_reasoning_effort == "medium"
    assert s.auto_apply_enabled is True
    assert s.application_dry_run is True
    assert s.default_language == "pt"


def test_resolve_model_and_effort_variants():
    assert resolve_model_and_effort("gemini-4-neon", "medium") == ("gemini-4-neon-medium", "medium")
    assert resolve_model_and_effort("Gemini 4 Neon", "medium") == ("gemini-4-neon-medium", "medium")
    assert resolve_model_and_effort("gemini-4-neon-medium", "invalid") == ("gemini-4-neon-medium", "medium")
    assert resolve_model_and_effort("gemini-4-neon-high", "high") == ("gemini-4-neon-high", "high")


def test_redact_secrets_and_json_formatter():
    raw = "POST https://api.telegram.org/bot123456:ABCDEF1234567890_secret_token/sendMessage token=mysecret"
    redacted = redact_secrets(raw)
    assert "ABCDEF1234567890" not in redacted
    assert "mysecret" not in redacted
    assert "REDACTED" in redacted

    formatter = JsonLogFormatter()
    rec = logging.LogRecord("jobbot.test", logging.INFO, __file__, 1, "hello token=topsecret", (), None)
    rec.job_id = "gh:123"
    parsed = json.loads(formatter.format(rec))
    assert parsed["logger"] == "jobbot.test"
    assert parsed["job_id"] == "gh:123"
    assert "topsecret" not in parsed["message"]


def test_run_lock_prevents_concurrent_execution(tmp_path: Path):
    lock_path = str(tmp_path / "jobbot.lock")
    with run_lock(lock_path):
        with pytest.raises(ConcurrencyError):
            with run_lock(lock_path):
                pass
    # Acquirable again after release
    with run_lock(lock_path):
        assert Path(lock_path).exists()
