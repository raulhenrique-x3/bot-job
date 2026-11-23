"""SQLite persistence for jobs, application lifecycle, idempotency, retries, and audit logs."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.filters.filters import content_hash

logger = logging.getLogger("jobbot.database")

APPLICATION_STATUSES = frozenset({
    "discovered",
    "new",  # legacy alias for discovered
    "analyzed",
    "rejected",
    "skipped_low_match",  # legacy alias for rejected
    "preparing_documents",
    "documents_ready",
    "pending_human_action",
    "applying",
    "applied",
    "application_failed",
    "analysis_failed",
    "notified",
    "expired",
})

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    apply_url TEXT DEFAULT '',
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
    status TEXT NOT NULL DEFAULT 'discovered',
    match_score INTEGER,
    recommendation TEXT,
    language TEXT,
    eliminatory_blockers TEXT,
    matched_skills TEXT,
    missing_skills TEXT,
    reasons TEXT,
    resume_key TEXT,
    resume_suggestions TEXT,
    analysis_json TEXT,
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_hash ON jobs(content_hash);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    apply_url TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'preparing_documents',
    match_score INTEGER,
    language TEXT NOT NULL DEFAULT 'pt',
    base_resume_key TEXT NOT NULL DEFAULT '',
    custom_resume_path TEXT NOT NULL DEFAULT '',
    cover_letter_path TEXT NOT NULL DEFAULT '',
    documents_dir TEXT NOT NULL DEFAULT '',
    confirmation_id TEXT NOT NULL DEFAULT '',
    failure_reason TEXT NOT NULL DEFAULT '',
    human_action_reason TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_retry_at TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT,
    notified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_apps_hash ON applications(content_hash);
CREATE INDEX IF NOT EXISTS idx_apps_applied_at ON applications(applied_at);

CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job_id ON application_events(job_id);

CREATE TABLE IF NOT EXISTS human_answers (
    job_id TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    answer_token TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'answer',
    source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (job_id, question)
);
"""

_JOB_MIGRATION_COLUMNS = (
    ("apply_url", "TEXT DEFAULT ''"),
    ("language", "TEXT"),
    ("eliminatory_blockers", "TEXT"),
    ("analysis_json", "TEXT"),
)


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # autocommit: each statement is durable immediately (bot may be killed any time)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for col_name, col_def in _JOB_MIGRATION_COLUMNS:
            if col_name not in cols:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_def}")

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Jobs table operations
    # ------------------------------------------------------------------
    def job_exists(self, job_id: str, chash: str) -> str | None:
        """Return existing row id if this job was already seen (by id or content hash)."""
        row = self._conn.execute(
            "SELECT id FROM jobs WHERE id = ? OR (content_hash = ? AND content_hash != '')",
            (job_id, chash),
        ).fetchone()
        return row["id"] if row else None

    def get_job(self, job_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def _normalize(self, job: dict) -> dict:
        data = dict(job)
        data["company"] = data.get("company") or ""
        data["location"] = data.get("location") or ""
        if (data.get("source") or "").strip().lower() == "linkedin_posts":
            data["apply_url"] = data.get("apply_url") or ""
        else:
            data["apply_url"] = data.get("apply_url") or data.get("url") or ""
        return data

    def insert_job(self, job: dict) -> None:
        data = self._normalize(job)
        data["content_hash"] = data.get("content_hash") or content_hash(
            data.get("title") or "", data.get("company") or ""
        )
        now = self._now()
        keys = [
            "id", "content_hash", "source", "url", "apply_url", "title", "company",
            "location", "modality", "seniority", "salary", "posted_at", "body",
        ]
        values = [data.get(k) or "" for k in keys]
        self._conn.execute(
            f"""INSERT INTO jobs ({', '.join(keys)}, first_seen_at, last_seen_at, status)
                VALUES ({', '.join('?' * len(keys))}, ?, ?, 'discovered')""",
            values + [now, now],
        )
        self.log_event(data["id"], "discovered", f"source={data.get('source', '')}")

    def update_job_body(self, job_id: str, body: str, apply_url: str | None = None) -> None:
        if apply_url:
            self._conn.execute(
                "UPDATE jobs SET body = ?, apply_url = ? WHERE id = ?",
                (body, apply_url, job_id),
            )
        else:
            self._conn.execute("UPDATE jobs SET body = ? WHERE id = ?", (body, job_id))

    def touch(self, job_id: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET last_seen_at = ? WHERE id = ?", (self._now(), job_id)
        )

    def set_status(self, job_id: str, status: str, detail: str = "") -> None:
        self._conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
        self.log_event(job_id, status, detail)

    def save_analysis(self, job_id: str, analysis: dict) -> None:
        self._conn.execute(
            """UPDATE jobs
               SET match_score = ?, recommendation = ?, language = ?,
                   eliminatory_blockers = ?, matched_skills = ?,
                   missing_skills = ?, reasons = ?, resume_key = ?,
                   resume_suggestions = ?, analysis_json = ?, status = 'analyzed'
               WHERE id = ?""",
            (
                analysis.get("match"),
                analysis.get("recommendation"),
                analysis.get("language", "pt"),
                json.dumps(analysis.get("eliminatory_blockers", []), ensure_ascii=False),
                json.dumps(analysis.get("matched_skills", []), ensure_ascii=False),
                json.dumps(analysis.get("missing_skills", []), ensure_ascii=False),
                json.dumps(analysis.get("reasons", []), ensure_ascii=False),
                analysis.get("resume_key"),
                json.dumps(analysis.get("resume_changes", []), ensure_ascii=False),
                json.dumps(analysis, ensure_ascii=False),
                job_id,
            ),
        )
        self.log_event(
            job_id,
            "analyzed",
            f"match={analysis.get('match')} rec={analysis.get('recommendation')} lang={analysis.get('language')}",
        )

    def get_analysis(self, job_id: str) -> dict | None:
        row = self.get_job(job_id)
        if not row:
            return None
        if row.get("analysis_json"):
            try:
                return json.loads(row["analysis_json"])
            except json.JSONDecodeError:
                pass
        if row.get("match_score") is None:
            return None
        return {
            "match": row["match_score"],
            "recommendation": row.get("recommendation") or "maybe",
            "language": row.get("language") or "pt",
            "eliminatory_blockers": json.loads(row.get("eliminatory_blockers") or "[]"),
            "matched_skills": json.loads(row.get("matched_skills") or "[]"),
            "missing_skills": json.loads(row.get("missing_skills") or "[]"),
            "reasons": json.loads(row.get("reasons") or "[]"),
            "resume_key": row.get("resume_key"),
            "resume_changes": json.loads(row.get("resume_suggestions") or "[]"),
            "seniority": row.get("seniority") or "",
        }

    def mark_notified(self, job_id: str) -> None:
        now = self._now()
        self._conn.execute(
            "UPDATE jobs SET status = 'notified', notified_at = ? WHERE id = ?",
            (now, job_id),
        )
        self.log_event(job_id, "notified", "")

    def jobs_pending_analysis(self, limit: int = 10) -> list[dict]:
        """Return newest pending jobs interleaved across sources so no portal (e.g. LinkedIn) is starved."""
        rows = self._conn.execute(
            """SELECT * FROM jobs WHERE status IN ('discovered', 'new')
               ORDER BY first_seen_at DESC
               LIMIT ?""",
            (max(limit * 10, 80),),
        ).fetchall()
        by_source: dict[str, list[dict]] = {}
        for r in rows:
            d = dict(r)
            by_source.setdefault(d.get("source") or "", []).append(d)
        sources = sorted(
            by_source.keys(),
            key=lambda s: (0 if s == "linkedin_posts" else 1 if s == "linkedin" else 2, s),
        )
        result: list[dict] = []
        while len(result) < limit and sources:
            next_sources = []
            for src in sources:
                bucket = by_source[src]
                if bucket and len(result) < limit:
                    result.append(bucket.pop(0))
                if bucket:
                    next_sources.append(src)
            sources = next_sources
        return result

    # ------------------------------------------------------------------
    # Applications lifecycle, idempotency, retries & recovery
    # ------------------------------------------------------------------
    def get_application(self, job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM applications WHERE job_id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def has_duplicate_application(self, job_id: str, chash: str, *, include_dry_run: bool = False) -> dict | None:
        """Idempotency guard: returns an existing final/active application by job_id or content_hash."""
        if include_dry_run:
            row = self._conn.execute(
                """SELECT * FROM applications
                   WHERE (job_id = ? OR (content_hash = ? AND content_hash != ''))
                     AND (
                         status IN ('applied', 'applying', 'pending_human_action')
                         OR (status = 'documents_ready' AND dry_run = 1 AND notified_at IS NOT NULL)
                     )
                   LIMIT 1""",
                (job_id, chash),
            ).fetchone()
        else:
            row = self._conn.execute(
                """SELECT * FROM applications
                   WHERE (job_id = ? OR (content_hash = ? AND content_hash != ''))
                     AND status IN ('applied', 'applying', 'pending_human_action')
                     AND dry_run = 0
                   LIMIT 1""",
                (job_id, chash),
            ).fetchone()
        return dict(row) if row else None

    def upsert_application(
        self,
        job: dict,
        *,
        status: str,
        match_score: int | None = None,
        language: str = "pt",
        base_resume_key: str = "",
        custom_resume_path: str = "",
        cover_letter_path: str = "",
        documents_dir: str = "",
        platform: str = "",
        apply_url: str = "",
        confirmation_id: str = "",
        failure_reason: str = "",
        human_action_reason: str = "",
        max_attempts: int = 3,
        dry_run: bool = False,
        applied_at: str | None = None,
    ) -> dict:
        now = self._now()
        job_id = job["id"]
        chash = job.get("content_hash") or content_hash(job.get("title") or "", job.get("company") or "")
        existing = self.get_application(job_id)

        if existing is None:
            self._conn.execute(
                """INSERT INTO applications (
                       job_id, content_hash, company, title, url, apply_url, platform,
                       status, match_score, language, base_resume_key, custom_resume_path,
                       cover_letter_path, documents_dir, confirmation_id, failure_reason,
                       human_action_reason, attempt_count, max_attempts, dry_run,
                       applied_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    chash,
                    job.get("company") or "",
                    job.get("title") or "",
                    job.get("url") or "",
                    apply_url or job.get("apply_url") or job.get("url") or "",
                    platform,
                    status,
                    match_score,
                    language,
                    base_resume_key,
                    custom_resume_path,
                    cover_letter_path,
                    documents_dir,
                    confirmation_id,
                    failure_reason,
                    human_action_reason,
                    max_attempts,
                    1 if dry_run else 0,
                    applied_at,
                    now,
                    now,
                ),
            )
        else:
            self._conn.execute(
                """UPDATE applications
                   SET apply_url = CASE WHEN ? != '' THEN ? ELSE apply_url END,
                       platform = CASE WHEN ? != '' THEN ? ELSE platform END,
                       status = ?,
                       match_score = COALESCE(?, match_score),
                       language = CASE WHEN ? != '' THEN ? ELSE language END,
                       base_resume_key = CASE WHEN ? != '' THEN ? ELSE base_resume_key END,
                       custom_resume_path = CASE WHEN ? != '' THEN ? ELSE custom_resume_path END,
                       cover_letter_path = CASE WHEN ? != '' THEN ? ELSE cover_letter_path END,
                       documents_dir = CASE WHEN ? != '' THEN ? ELSE documents_dir END,
                       confirmation_id = CASE WHEN ? != '' THEN ? ELSE confirmation_id END,
                       failure_reason = ?,
                       human_action_reason = ?,
                       max_attempts = ?,
                       dry_run = ?,
                       applied_at = COALESCE(?, applied_at),
                       updated_at = ?
                   WHERE job_id = ?""",
                (
                    apply_url, apply_url,
                    platform, platform,
                    status,
                    match_score,
                    language, language,
                    base_resume_key, base_resume_key,
                    custom_resume_path, custom_resume_path,
                    cover_letter_path, cover_letter_path,
                    documents_dir, documents_dir,
                    confirmation_id, confirmation_id,
                    failure_reason,
                    human_action_reason,
                    max_attempts,
                    1 if dry_run else 0,
                    applied_at,
                    now,
                    job_id,
                ),
            )
        self._conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
        detail = failure_reason or human_action_reason or confirmation_id or platform
        self.log_event(job_id, status, detail)
        return self.get_application(job_id) or {}

    def record_application_attempt(self, job_id: str) -> int:
        now = self._now()
        self._conn.execute(
            """UPDATE applications
               SET attempt_count = attempt_count + 1, status = 'applying', updated_at = ?
               WHERE job_id = ?""",
            (now, job_id),
        )
        self._conn.execute("UPDATE jobs SET status = 'applying' WHERE id = ?", (job_id,))
        row = self.get_application(job_id)
        count = int(row["attempt_count"]) if row else 1
        self.log_event(job_id, "applying", f"attempt={count}")
        return count

    def record_application_success(
        self,
        job_id: str,
        *,
        platform: str,
        apply_url: str = "",
        confirmation_id: str = "",
        dry_run: bool = False,
    ) -> dict:
        now = self._now()
        final_status = "documents_ready" if dry_run else "applied"
        self._conn.execute(
            """UPDATE applications
               SET status = ?,
                   platform = CASE WHEN ? != '' THEN ? ELSE platform END,
                   apply_url = CASE WHEN ? != '' THEN ? ELSE apply_url END,
                   confirmation_id = ?,
                   failure_reason = '',
                   human_action_reason = '',
                   next_retry_at = NULL,
                   dry_run = ?,
                   applied_at = CASE WHEN ? = 1 THEN applied_at ELSE ? END,
                   updated_at = ?
               WHERE job_id = ?""",
            (
                final_status,
                platform, platform,
                apply_url, apply_url,
                confirmation_id,
                1 if dry_run else 0,
                1 if dry_run else 0,
                now,
                now,
                job_id,
            ),
        )
        self._conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (final_status, job_id))
        self.log_event(
            job_id,
            final_status,
            f"platform={platform} dry_run={dry_run} confirmation={confirmation_id}",
        )
        return self.get_application(job_id) or {}

    def record_application_failure(
        self,
        job_id: str,
        reason: str,
        *,
        retryable: bool = True,
        backoff_minutes: int = 30,
    ) -> tuple[dict, bool]:
        """Record failure and schedule controlled retry if attempts remain.

        Returns (application_row, exhausted_retries).
        """
        app = self.get_application(job_id)
        attempts = int(app["attempt_count"]) if app else 1
        max_attempts = int(app["max_attempts"]) if app else 3
        now_dt = datetime.now(timezone.utc)

        next_retry: str | None = None
        exhausted = True
        if retryable and attempts < max_attempts:
            delay = backoff_minutes * (2 ** max(0, attempts - 1))
            next_retry = (now_dt + timedelta(minutes=delay)).isoformat()
            exhausted = False

        self._conn.execute(
            """UPDATE applications
               SET status = 'application_failed',
                   failure_reason = ?,
                   next_retry_at = ?,
                   updated_at = ?
               WHERE job_id = ?""",
            (reason[:500], next_retry, now_dt.isoformat(), job_id),
        )
        self._conn.execute("UPDATE jobs SET status = 'application_failed' WHERE id = ?", (job_id,))
        self.log_event(job_id, "application_failed", f"{reason[:200]} next_retry={next_retry or 'none'}")
        return self.get_application(job_id) or {}, exhausted

    def mark_application_notified(self, job_id: str) -> None:
        now = self._now()
        self._conn.execute(
            "UPDATE applications SET notified_at = ?, updated_at = ? WHERE job_id = ?",
            (now, now, job_id),
        )
        self._conn.execute("UPDATE jobs SET notified_at = ? WHERE id = ?", (now, job_id))

    def count_applications_today(self) -> int:
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        row = self._conn.execute(
            """SELECT COUNT(*) AS n FROM applications
               WHERE status = 'applied' AND dry_run = 0 AND applied_at >= ?""",
            (day_start,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def jobs_ready_for_application(self, limit: int = 5) -> list[dict]:
        """Return analyzed/documents_ready jobs plus retry-eligible failed applications."""
        now = self._now()
        rows = self._conn.execute(
            """SELECT j.* FROM jobs j
               LEFT JOIN applications a ON a.job_id = j.id
               WHERE (
                   j.status IN ('analyzed', 'documents_ready')
                   AND (a.job_id IS NULL OR a.status IN ('documents_ready', 'preparing_documents'))
               ) OR (
                   j.status = 'application_failed'
                   AND a.next_retry_at IS NOT NULL
                   AND a.next_retry_at <= ?
                   AND a.attempt_count < a.max_attempts
               )
               ORDER BY j.match_score DESC, j.first_seen_at ASC
               LIMIT ?""",
            (now, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def recover_interrupted(self) -> dict[str, int]:
        """Recover from an unexpected crash on previous run safely without duplicate submits."""
        now = self._now()
        # preparing_documents is safe to resume from 'analyzed'
        prep_rows = self._conn.execute(
            "SELECT job_id FROM applications WHERE status = 'preparing_documents'"
        ).fetchall()
        for r in prep_rows:
            self._conn.execute(
                "UPDATE applications SET status = 'analyzed', updated_at = ? WHERE job_id = ?",
                (now, r["job_id"]),
            )
            self._conn.execute("UPDATE jobs SET status = 'analyzed' WHERE id = ?", (r["job_id"],))
            self.log_event(r["job_id"], "analyzed", "recovered from interrupted preparing_documents")

        # applying might have already submitted before crash -> escalate safely to pending_human_action
        applying_rows = self._conn.execute(
            "SELECT job_id FROM applications WHERE status = 'applying'"
        ).fetchall()
        for r in applying_rows:
            reason = "Processo interrompido durante envio; verificar manualmente para evitar duplicidade."
            self._conn.execute(
                """UPDATE applications
                   SET status = 'pending_human_action', human_action_reason = ?, updated_at = ?
                   WHERE job_id = ?""",
                (reason, now, r["job_id"]),
            )
            self._conn.execute(
                "UPDATE jobs SET status = 'pending_human_action' WHERE id = ?", (r["job_id"],)
            )
            self.log_event(r["job_id"], "pending_human_action", "recovered from interrupted applying")

        return {"preparing_recovered": len(prep_rows), "applying_escalated": len(applying_rows)}

    def log_event(self, job_id: str, status: str, detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO application_events (job_id, status, detail, created_at) VALUES (?, ?, ?, ?)",
            (job_id, status, (detail or "")[:500], self._now()),
        )

    def events_for(self, job_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM application_events WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def application_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ------------------------------------------------------------------
    # Candidate-confirmed answers (documentation of human answers)
    # ------------------------------------------------------------------
    def record_human_answer(
        self,
        job_id: str,
        question: str,
        answer: str = "",
        *,
        kind: str = "answer",
        source: str = "",
        answer_token: str = "",
    ) -> None:
        """Store a candidate-confirmed answer for a question (human answers are authoritative)."""
        now = self._now()
        self._conn.execute(
            """INSERT INTO human_answers (job_id, question, answer, answer_token, kind, source, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id, question) DO UPDATE SET
                   answer = excluded.answer,
                   answer_token = excluded.answer_token,
                   kind = excluded.kind,
                   source = excluded.source,
                   updated_at = excluded.updated_at""",
            (job_id, question, answer, answer_token, kind, source, now),
        )
        self.log_event(job_id, "human_answer", f"{question[:80]}")

    def get_human_answers(self, job_id: str) -> dict[str, str]:
        """Return {question: answer} for candidate-confirmed answers of a job."""
        rows = self._conn.execute(
            "SELECT question, answer FROM human_answers WHERE job_id = ? AND kind = 'answer'",
            (job_id,),
        ).fetchall()
        return {r["question"]: r["answer"] for r in rows}

    def get_human_answers_full(self, job_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT job_id, question, answer, answer_token, kind, source, updated_at "
            "FROM human_answers WHERE job_id = ? ORDER BY question",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def reset_app_for_retry(self, job_id: str, *, note: str = "") -> dict:
        """Reset a pending_human_action application so the cycle re-attempts the submission."""
        now = self._now()
        detail = note or "Candidatura reativada a partir da resposta humana confirmada."
        self._conn.execute(
            """UPDATE applications
               SET status = 'documents_ready', failure_reason = '', human_action_reason = ?,
                   next_retry_at = NULL, notified_at = NULL, updated_at = ?
               WHERE job_id = ?""",
            (detail, now, job_id),
        )
        self._conn.execute(
            "UPDATE jobs SET status = 'documents_ready', notified_at = NULL WHERE id = ?",
            (job_id,),
        )
        self.log_event(job_id, "documents_ready", f"reset for retry: {note[:120]}")
        return self.get_application(job_id) or {}
