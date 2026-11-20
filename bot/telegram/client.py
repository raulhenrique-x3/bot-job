"""Thin Telegram Bot API client (stdlib urllib, multipart PDF uploads, secret-safe logging)."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

logger = logging.getLogger("jobbot.telegram")

API_BASE = "https://api.telegram.org"


class TelegramClient:
    """Thin Bot API client. Swallows network errors (logging only)."""

    def __init__(self, token: str | None = None, chat_id: str | None = None):
        tok = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        cid = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self._chat_id = cid
        self._base = f"{API_BASE}/bot{tok}"
        self._enabled = bool(tok and cid)
        if not self._enabled:
            logger.warning("Telegram credentials missing — notifications disabled")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _call(self, method: str, *, data: dict | None = None, multipart: list | None = None):
        if not self._enabled:
            return None
        try:
            if multipart:
                body, content_type = self._encode_multipart(data or {}, multipart)
                req = urllib.request.Request(
                    f"{self._base}/{method}",
                    data=body,
                    headers={"Content-Type": content_type},
                )
            else:
                payload = urllib.parse.urlencode(data or {}).encode()
                req = urllib.request.Request(
                    f"{self._base}/{method}",
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("Telegram %s failed: %s", method, exc)
            return None

    @staticmethod
    def _encode_multipart(data: dict, files: list[dict]) -> tuple[bytes, str]:
        boundary = uuid.uuid4().hex
        out = []
        for key, value in data.items():
            out.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
            )
        for f in files:
            name, path = f["name"], f["path"]
            filename = f.get("filename") or Path(path).name
            mime = mimetypes.guess_type(path)[0] or "application/pdf"
            with open(path, "rb") as fh:
                content = fh.read()
            out.append(
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                    f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n"
                ).encode()
                + content
                + b"\r\n"
            )
        out.append(f"--{boundary}--\r\n".encode())
        return b"".join(out), f"multipart/form-data; boundary={boundary}"

    def send_message(self, text: str) -> bool:
        return self._call(
            "sendMessage",
            data={
                "chat_id": self._chat_id,
                "text": text[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        ) is not None

    def send_document(self, path: str, caption: str | None = None, filename: str | None = None) -> bool:
        if not path or not os.path.exists(path):
            logger.warning("Document file missing: %s", path)
            return False
        data = {"chat_id": self._chat_id}
        if caption:
            data["caption"] = caption[:1024]
        return self._call(
            "sendDocument",
            data=data,
            multipart=[{"name": "document", "path": path, "filename": filename or Path(path).name}],
        ) is not None

    def send_job(self, message: str, resume_path: str | None) -> bool:
        """Backwards-compatible: send recommendation message with base resume PDF."""
        if resume_path and os.path.exists(resume_path):
            self.send_document(resume_path)
        elif resume_path:
            logger.warning("Resume file missing: %s", resume_path)
        return self.send_message(message)

    def send_application_bundle(
        self,
        message: str,
        *,
        resume_path: str | None = None,
        cover_letter_path: str | None = None,
        job_slug: str = "",
    ) -> bool:
        """Send structured application notification followed by tailored PDF attachments."""
        ok = self.send_message(message)
        if resume_path and os.path.exists(resume_path):
            self.send_document(
                resume_path,
                caption="Currículo personalizado (ATS)",
                filename=Path(resume_path).name,
            )
        if cover_letter_path and os.path.exists(cover_letter_path):
            self.send_document(
                cover_letter_path,
                caption="Carta de apresentação personalizada",
                filename=Path(cover_letter_path).name,
            )
        return ok

    def send_alert(self, message: str) -> bool:
        """Send an operational or critical failure alert."""
        return self.send_message(message)
