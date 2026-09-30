import logging
import os
import sys

from dotenv import load_dotenv

from bot.config import env_int, setup_logging
from bot.database.db import Database
from bot.filters.filters import (
    content_hash,
    is_too_old,
    passes_pre_filters,
)
from bot.gemini.analysis import JobAnalyzer
from bot.profile import ResumeLibrary
from bot.scrapers.geekhunter import GeekHunterScraper
from bot.scrapers.linkedin import LinkedInScraper
from bot.scrapers.nerdin import NerdinScraper
from bot.scrapers.programathor import ProgramathorScraper
from bot.scrapers.querovagas import QueroVagasScraper
from bot.telegram.client import TelegramClient
from bot.telegram.format import format_job_message

logger = logging.getLogger("jobbot.main")

MAX_ANALYSES_PER_RUN = 8


def build_scrapers(browser, max_age_days: int) -> list:
    from bot.config import env, env_list

    scrapers = []
    from bot.config import env_bool

    if env_bool("PROGRAMATHOR_ENABLED", True):
        scrapers.append(ProgramathorScraper(browser))
    if env_bool("GEEKHUNTER_ENABLED", True):
        scrapers.append(GeekHunterScraper(browser))
    if env_bool("LINKEDIN_ENABLED", True):
        terms = env_list("LINKEDIN_SEARCH_TERMS", "desenvolvedor")
        scrapers.append(
            LinkedInScraper(
                browser,
                search_terms=terms,
                max_days=max_age_days,
                remote_only=False,
            )
        )
    if env_bool("QUEROVAGAS_ENABLED", True):
        scrapers.append(QueroVagasScraper(browser, work_mode=env("QUEROVAGAS_WORK_MODE", "Remote")))
    if env_bool("NERDIN_ENABLED", True):
        scrapers.append(NerdinScraper(browser))
    return scrapers


def normalize(job: dict) -> dict:
    title = (job.get("title") or "").strip()
    data = dict(job)
    data["title"] = title
    return data


def collect_jobs(scrapers) -> list[dict]:
    all_jobs: list[dict] = []
    for scraper in scrapers:
        try:
            jobs = scraper.scrape()
            logger.info("scraper=%s collected=%d", scraper.name, len(jobs))
            all_jobs.extend(jobs)
        except Exception as exc:  # noqa: BLE001
            logger.error("scraper=%s failed err=%s", scraper.name, exc)
    return all_jobs


def dedup_and_filter(all_jobs: list[dict], db: Database, max_age_days: int) -> list[dict]:
    fresh: list[dict] = []
    seen: set[str] = set()
    for job in all_jobs:
        if not job.get("title") or not job.get("url"):
            continue
        jid = job["id"]
        chash = content_hash(job["title"], job.get("company") or "")
        if jid in seen or chash in seen:
            continue
        existing = db.job_exists(jid, chash)
        if existing:
            db.touch(existing)
            seen.add(jid)
            seen.add(chash)
            continue
        if is_too_old(job.get("posted_at"), max_age_days):
            logger.debug("too old: %s", job["title"])
            continue
        ok, reason = passes_pre_filters(job)
        if not ok:
            logger.debug("filtered out (%s): %s", reason, job["title"])
            continue
        seen.add(jid)
        seen.add(chash)
        fresh.append(job)
    return fresh


def persist(jobs: list[dict], db: Database) -> list[dict]:
    """Insert new jobs. Detail bodies are fetched lazily at analysis time."""
    inserted: list[dict] = []
    for job in jobs:
        chash = content_hash(job["title"], job.get("company") or "")
        if db.job_exists(job["id"], chash):
            continue
        if job.get("expired"):
            continue
        db.insert_job(job)
        inserted.append(job)
    return inserted


def run_once() -> int:
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    setup_logging("INFO")
    logger.info("=== jobbot run start ===")

    max_age_days = env_int("JOB_MAX_AGE_DAYS", 14)

    from playwright.sync_api import sync_playwright

    from bot.scrapers.browser import launch_options

    with sync_playwright() as pw:
        storage = "state/auth_linkedin.json" if os.path.exists("state/auth_linkedin.json") else None
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx_opts = dict(launch_options())
        if storage:
            ctx_opts["storage_state"] = storage
        context = browser.new_context(**ctx_opts)
        scrapers = [
            s for s in build_scrapers(context, max_age_days)
        ]
        db = Database("data/jobs.db")
        telegram = TelegramClient()

        try:
            all_jobs = collect_jobs(scrapers)
            fresh = dedup_and_filter(all_jobs, db, max_age_days)
            logger.info("collected=%d fresh=%d", len(all_jobs), len(fresh))
            if fresh:
                new_jobs = persist(fresh, db)
                logger.info("new_jobs=%d", len(new_jobs))

            resume_library = ResumeLibrary("resumes")
            analyzer = JobAnalyzer(resume_library)
            if not analyzer.ready:
                logger.warning("no analyzer — finishing run without analysis")
                return 0
            by_source = {s.name: s for s in scrapers}
            pending = db.jobs_pending_analysis(limit=MAX_ANALYSES_PER_RUN)
            logger.info("pending_analyses=%d", len(pending))

            notified = 0
            for job_row in pending:
                job = normalize(job_row)
                if not job.get("body"):
                    scraper = by_source.get(job.get("source"))
                    if scraper and hasattr(scraper, "fetch_detail"):
                        try:
                            detail = scraper.fetch_detail(job["url"])
                            job["body"] = detail.get("body", "")
                            if detail.get("expired"):
                                db.set_status(job["id"], "expired")
                                continue
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("detail err=%s id=%s", exc, job["id"])
                analysis = analyzer.analyze(job)
                if analysis is None:
                    db.set_status(job["id"], "analysis_failed")
                    continue
                db.save_analysis(job["id"], analysis)
                match = analysis.get("match") or 0
                threshold = env_int("JOB_MATCH_THRESHOLD", 75)
                if match >= threshold:
                    resume_path = None
                    resume_key = analysis.get("resume_key")
                    if resume_key:
                        resume_path = str(resume_library.path_for(resume_key))
                    message = format_job_message(job, analysis)
                    telegram.send_job(message, resume_path)
                    db.mark_notified(job["id"])
                    notified += 1
                    logger.info(
                        "notified id=%s match=%d resume=%s",
                        job["id"], match, resume_key,
                    )
                else:
                    db.set_status(job["id"], "skipped_low_match")
                    logger.info("skipped id=%s match=%d", job["id"], match)
            logger.info("notified=%d stats=%s", notified, db.stats())
            return notified
        finally:
            db.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(0 if run_once() is not None else 1)
