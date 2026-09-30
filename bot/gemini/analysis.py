import json
import logging
import os
import re
import shutil
import subprocess

logger = logging.getLogger("jobbot.gemini")

JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "match": {"type": "integer", "minimum": 0, "maximum": 100},
            "recommendation": {"type": "string", "enum": ["apply", "maybe", "skip"]},
            "matched_skills": {"type": "array", "items": {"type": "string"}},
            "missing_skills": {"type": "array", "items": {"type": "string"}},
            "reasons": {"type": "array", "items": {"type": "string"}},
            "resume_key": {"type": "string"},
            "resume_changes": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "match", "recommendation", "matched_skills", "missing_skills",
            "reasons", "resume_key", "resume_changes",
        ],
    }
)


def agy_available() -> bool:
    return shutil.which("agy") is not None


def agy_ask(prompt: str, timeout: int = 300) -> str:
    binary = shutil.which("agy")
    if not binary:
        raise RuntimeError("agy not found in PATH (~/.local/bin/agy)")
    cmd = [binary, "-p", prompt, "--output-format", "json",
           "--json-schema", JSON_SCHEMA]
    model = os.environ.get("AGY_MODEL")
    if model:
        cmd.extend(["--model", model])
    resp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if resp.returncode != 0:
        raise RuntimeError(resp.stderr.strip() or f"agy exit {resp.returncode}")
    try:
        wrapper = json.loads(resp.stdout)
    except json.JSONDecodeError:
        return resp.stdout.strip()
    usage = wrapper.get("usage") or {}
    logger.info(
        "agy tokens in=%s out=%s think=%s dur=%.1fs",
        usage.get("input_tokens"), usage.get("output_tokens"),
        usage.get("thinking_tokens"), wrapper.get("duration_seconds") or -1,
    )
    return (wrapper.get("response") or "").strip()


class JobAnalyzer:
    """Analyzes job fit via the agy CLI (no API key needed)."""

    def __init__(self, resume_library):
        self._resumes = resume_library
        self._ready = agy_available()
        if not self._ready:
            logger.warning("agy not available — analysis disabled, jobs stay 'new'")

    @property
    def ready(self) -> bool:
        return self._ready

    def analyze(self, job: dict) -> dict | None:
        if not self._ready:
            return None
        from bot.gemini.prompts import ANALYSIS_SYSTEM, ANALYSIS_USER

        prompt = (
            f"{ANALYSIS_SYSTEM}\n\n{ANALYSIS_USER.format(**self._prompt_kwargs(job))}"
        )
        try:
            raw = agy_ask(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.error("agy call failed job=%s err=%s", job.get("id"), exc)
            return None
        data = self._parse(raw)
        if data is None:
            return None
        data["resume_key"] = self._validate_resume_key(data.get("resume_key"))
        return data

    def _prompt_kwargs(self, job: dict) -> dict:
        return {
            "resumes": self._resumes.as_prompt_block(),
            "source": job.get("source", ""),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "url": job.get("url", ""),
            "body": (job.get("body") or "")[:7000],
        }

    @staticmethod
    def _parse(raw: str) -> dict | None:
        if not raw:
            logger.warning("agy returned empty response")
            return None
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            logger.warning("agy response has no JSON object")
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("agy returned non-JSON response")
            return None
        score = data.get("match")
        if not isinstance(score, int) or not 0 <= score <= 100:
            return None
        rec = data.get("recommendation")
        if rec not in ("apply", "maybe", "skip"):
            rec = "apply" if score >= 75 else "maybe" if score >= 60 else "skip"
        data["recommendation"] = rec
        allowed = {"match", "recommendation", "matched_skills", "missing_skills",
                   "reasons", "resume_key", "resume_changes"}
        data = {k: v for k, v in data.items() if k in allowed}
        for field in ("matched_skills", "missing_skills", "reasons", "resume_changes"):
            data[field] = [str(x)[:160] for x in (data.get(field) or [])][:8]
        return data

    def _validate_resume_key(self, key) -> str | None:
        valid = self._resumes.keys()
        if key in valid:
            return key
        lowered = (key or "").lower()
        for v in valid:
            if lowered and (lowered in v.lower() or v.lower() in lowered):
                return v
        logger.warning("AI returned unknown resume_key %r, using fallback", key)
        return valid[0] if valid else None
