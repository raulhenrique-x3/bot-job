"""Tests for expanded SQLite schema, migrations, lifecycle, idempotency, retries, and crash recovery."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.database.db import Database


def _sample_job(jid: str = "gh:100", title: str = "Backend Python Engineer", company: str = "Acme") -> dict:
    return {
        "id": jid,
        "source": "geekhunter",
        "url": f"https://boards.greenhouse.io/acme/jobs/{jid.split(':')[-1]}",
        "title": title,
        "company": company,
        "location": "Remoto",
        "modality": "Remoto",
        "seniority": "Pleno",
        "salary": "R$ 12.000",
        "body": "Vaga Python FastAPI React Docker.",
    }


def test_legacy_database_migrates_cleanly(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE jobs (
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
        INSERT INTO jobs (id, content_hash, source, url, title, first_seen_at, last_seen_at, status)
        VALUES ('old:1', 'abc123', 'programathor', 'https://example.com/1', 'Dev', '2026-01-01', '2026-01-01', 'new');
        """
    )
    conn.close()

    db = Database(str(db_path))
    try:
        pending = db.jobs_pending_analysis(limit=5)
        assert len(pending) == 1
        assert pending[0]["id"] == "old:1"
        assert "apply_url" in pending[0]
        assert "analysis_json" in pending[0]
    finally:
        db.close()


def test_full_application_lifecycle_and_idempotency(temp_db: Database):
    job1 = _sample_job("gh:101", "Fullstack Engineer", "Acme Corp")
    job2_same_role = _sample_job("li:999", "Fullstack Engineer", "Acme Corp")
    temp_db.insert_job(job1)
    assert temp_db.get_job("gh:101")["status"] == "discovered"

    temp_db.save_analysis(
        "gh:101",
        {
            "match": 88,
            "recommendation": "apply",
            "language": "en",
            "eliminatory_blockers": [],
            "matched_skills": ["Python", "React"],
            "missing_skills": [],
            "reasons": ["Strong fullstack match"],
            "resume_key": "resume_raul_fullstack",
            "resume_changes": ["Highlight FastAPI"],
        },
    )
    assert temp_db.get_job("gh:101")["status"] == "analyzed"

    temp_db.upsert_application(
        job1,
        status="documents_ready",
        match_score=88,
        language="en",
        base_resume_key="resume_raul_fullstack",
        custom_resume_path="data/applications/gh_101/resume.pdf",
        documents_dir="data/applications/gh_101",
        dry_run=False,
    )
    assert temp_db.record_application_attempt("gh:101") == 1
    app = temp_db.record_application_success(
        "gh:101", platform="greenhouse", confirmation_id="CONF-42", dry_run=False
    )
    assert app["status"] == "applied"
    assert app["confirmation_id"] == "CONF-42"
    assert temp_db.count_applications_today() == 1

    # Idempotency catches both same job_id AND same title+company hash from another portal
    from bot.filters.filters import content_hash

    ch = content_hash(job2_same_role["title"], job2_same_role["company"])
    assert temp_db.has_duplicate_application("gh:101", ch) is not None
    assert temp_db.has_duplicate_application("li:999", ch) is not None
    assert len(temp_db.events_for("gh:101")) >= 5


def test_controlled_retries_and_exhaustion(temp_db: Database):
    job = _sample_job("gh:202")
    temp_db.insert_job(job)
    temp_db.save_analysis("gh:202", {"match": 85, "recommendation": "apply", "language": "pt"})
    temp_db.upsert_application(job, status="documents_ready", max_attempts=2)

    temp_db.record_application_attempt("gh:202")
    app1, exhausted1 = temp_db.record_application_failure("gh:202", "timeout", retryable=True, backoff_minutes=10)
    assert exhausted1 is False
    assert app1["next_retry_at"] is not None

    # Force next_retry_at to past and ensure jobs_ready_for_application picks it up
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    temp_db._conn.execute("UPDATE applications SET next_retry_at = ? WHERE job_id = 'gh:202'", (past,))
    ready = temp_db.jobs_ready_for_application(limit=5)
    assert [r["id"] for r in ready] == ["gh:202"]

    temp_db.record_application_attempt("gh:202")
    app2, exhausted2 = temp_db.record_application_failure("gh:202", "timeout again", retryable=True, backoff_minutes=10)
    assert exhausted2 is True
    assert app2["next_retry_at"] is None
    assert temp_db.jobs_ready_for_application(limit=5) == []


def test_crash_recovery_handles_preparing_and_applying_safely(temp_db: Database):
    j_prep = _sample_job("gh:301", "Dev A", "Comp A")
    j_apply = _sample_job("gh:302", "Dev B", "Comp B")
    temp_db.insert_job(j_prep)
    temp_db.insert_job(j_apply)

    temp_db.upsert_application(j_prep, status="preparing_documents")
    temp_db.upsert_application(j_apply, status="applying")

    recovered = temp_db.recover_interrupted()
    assert recovered == {"preparing_recovered": 1, "applying_escalated": 1}
    assert temp_db.get_application("gh:301")["status"] == "analyzed"
    assert temp_db.get_application("gh:302")["status"] == "pending_human_action"
