import logging
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

logger = logging.getLogger("jobbot.telegram")

API_BASE = "https://api.telegram.org"


class TelegramClient:
    """Thin Bot API client. Swallows network errors (logging only)."""

    def __init__(self):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self._base = f"{API_BASE}/bot{token}"
        self._enabled = bool(token and self._chat_id)
        if not self._enabled:
            logger.warning("Telegram credentials missing — notifications disabled")

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
            mime = mimetypes.guess_type(path)[0] or "application/pdf"
            with open(path, "rb") as fh:
                content = fh.read()
            out.append(
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                    f"filename=\"{path.split('/')[-1]}\"\r\nContent-Type: {mime}\r\n\r\n"
                ).encode() + content + b"\r\n"
            )
        out.append(f"--{boundary}--\r\n".encode())
        return b"".join(out), f"multipart/form-data; boundary={boundary}"

    def send_message(self, text: str) -> None:
        self._call(
            "sendMessage",
            data={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"},
        )

    def send_document(self, path: str, caption: str | None = None) -> None:
        data = {"chat_id": self._chat_id}
        if caption:
            data["caption"] = caption[:1024]
        self._call("sendDocument", data=data, multipart=[{"name": "document", "path": path}])

    def send_job(self, message: str, resume_path: str | None) -> None:
        """Send job analysis message; the resume PDF goes before it as document."""
        if resume_path and os.path.exists(resume_path):
            self.send_document(resume_path)
        else:
            logger.warning("Resume file missing: %s", resume_path)
        self.send_message(message)
