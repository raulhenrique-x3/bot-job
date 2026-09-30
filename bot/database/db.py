import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bot.filters.filters import content_hash

logger = logging.getLogger("jobbot.database")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    modality TEXT,
    seniority TEXT,
    salary TEXT,
    posted_at TEXT,
    body TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    match_score INTEGER,
    recommendation TEXT,
    matched_skills TEXT,
    missing_skills TEXT,
    reasons TEXT,
    resume_key TEXT,
    resume_suggestions TEXT,
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_hash ON jobs(content_hash);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # autocommit: each statement is durable immediately (bot may be killed any time)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def job_exists(self, job_id: str, content_hash: str) -> str | None:
        """Return existing row id if this job was already seen (by id or content hash)."""
        row = self._conn.execute(
            "SELECT id FROM jobs WHERE id = ? OR (content_hash = ? AND content_hash != '')",
            (job_id, content_hash),
        ).fetchone()
        return row["id"] if row else None

    def _normalize(self, job: dict) -> dict:
        data = dict(job)
        if not data.get("company"):
            data["company"] = ""
        if not data.get("location"):
            data["location"] = ""
        return data

    def insert_job(self, job: dict) -> None:
        data = self._normalize(job)
        data["content_hash"] = data.get("content_hash") or content_hash(
            data.get("title") or "", data.get("company") or ""
        )
        now = self._now()
        keys = [
            "id", "content_hash", "source", "url", "title", "company", "location",
            "modality", "seniority", "salary", "posted_at", "body",
        ]
        values = [data.get(k) or "" for k in keys]
        self._conn.execute(
            f"""INSERT INTO jobs ({', '.join(keys)}, first_seen_at, last_seen_at, status)
                VALUES ({', '.join('?' * len(keys))}, ?, ?, 'new')""",
            values + [now, now],
        )

    def touch(self, job_id: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET last_seen_at = ? WHERE id = ?", (self._now(), job_id)
        )

    def set_status(self, job_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ? WHERE id = ?", (status, job_id)
        )

    def save_analysis(self, job_id: str, analysis: dict) -> None:
        import json

        self._conn.execute(
            """UPDATE jobs
               SET match_score = ?, recommendation = ?, matched_skills = ?,
                   missing_skills = ?, reasons = ?, resume_key = ?,
                   resume_suggestions = ?, status = 'analyzed'
               WHERE id = ?""",
            (
                analysis.get("match"),
                analysis.get("recommendation"),
                json.dumps(analysis.get("matched_skills", []), ensure_ascii=False),
                json.dumps(analysis.get("missing_skills", []), ensure_ascii=False),
                json.dumps(analysis.get("reasons", []), ensure_ascii=False),
                analysis.get("resume_key"),
                json.dumps(analysis.get("resume_changes", []), ensure_ascii=False),
                job_id,
            ),
        )

    def mark_notified(self, job_id: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = 'notified', notified_at = ? WHERE id = ?",
            (self._now(), job_id),
        )

    def jobs_pending_analysis(self, limit: int = 10) -> list[dict]:
        """Jobs with bodies first (no detail fetch needed), then oldest."""
        rows = self._conn.execute(
            """SELECT * FROM jobs WHERE status = 'new'
               ORDER BY (body IS NULL OR body = ''), first_seen_at
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
