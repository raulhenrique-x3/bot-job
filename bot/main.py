"""Main orchestration cycle: scrape -> filter -> analyze -> generate docs -> apply -> notify."""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

from bot.appliers import ApplicationDocuments, ApplierRegistry
from bot.config import (
    ConcurrencyError,
    Settings,
    env,
    env_bool,
    env_list,
    load_env,
    run_lock,
    setup_logging,
)
from bot.database.db import Database
from bot.documents import (
    ApplicationStorage,
    CoverLetterGenerator,
    TailoredResumeGenerator,
    sanitize_job_id,
    validate_pdf,
)
from bot.filters.filters import content_hash, international_fit_score, is_too_old, passes_pre_filters
from bot.gemini.analysis import JobAnalyzer, should_apply
from bot.profile import CandidateProfile, ResumeLibrary
from bot.scrapers.geekhunter import GeekHunterScraper
from bot.scrapers.international import (
    HimalayasScraper,
    JobicyScraper,
    RemotiveScraper,
    WeWorkRemotelyScraper,
    WorkingNomadsScraper,
)
from bot.scrapers.linkedin import LinkedInScraper, build_profile_search_terms
from bot.scrapers.nerdin import NerdinScraper
from bot.scrapers.programathor import ProgramathorScraper
from bot.scrapers.querovagas import QueroVagasScraper
from bot.telegram.client import TelegramClient
from bot.telegram.format import (
    format_application_notification,
    format_failure_alert,
    format_job_message,
)

logger = logging.getLogger("jobbot.main")


def _alert_marker_dir() -> Path:
    return Path("state/alerts")


def _alert_once(telegram, key: str, title: str, detail: str) -> bool:
    """Send a Telegram alert once per condition; repeats only after _clear_alert."""
    marker = _alert_marker_dir() / key
    if marker.exists():
        return False
    marker.parent.mkdir(parents=True, exist_ok=True)
    sent = telegram.send_alert(format_failure_alert(title, detail))
    if sent:
        marker.write_text("sent", encoding="utf-8")
    return sent


def _clear_alert(key: str) -> None:
    try:
        (_alert_marker_dir() / key).unlink(missing_ok=True)
    except OSError:
        pass


def build_scrapers(browser, max_age_days: int, *, profile: CandidateProfile | None = None) -> list:
    scrapers = []
    if env_bool("PROGRAMATHOR_ENABLED", True):
        scrapers.append(ProgramathorScraper(browser))
    if env_bool("GEEKHUNTER_ENABLED", True):
        scrapers.append(GeekHunterScraper(browser))
    if env_bool("LINKEDIN_ENABLED", True):
        if env_bool("LINKEDIN_USE_PROFILE_QUERIES", True) and profile is not None:
            terms = build_profile_search_terms(profile)
        else:
            terms = env_list("LINKEDIN_SEARCH_TERMS", "desenvolvedor")
        scrapers.append(
            LinkedInScraper(
                browser,
                search_terms=terms,
                max_days=max_age_days,
                remote_only=env_bool("LINKEDIN_REMOTE_ONLY", True),
                easy_apply_only=env_bool("LINKEDIN_EASY_APPLY_ONLY", True),
                location=env("LINKEDIN_LOCATION", "Brasil"),
            )
        )
    if env_bool("QUEROVAGAS_ENABLED", True):
        scrapers.append(QueroVagasScraper(browser, work_mode=env("QUEROVAGAS_WORK_MODE", "Remote")))
    if env_bool("NERDIN_ENABLED", True):
        scrapers.append(NerdinScraper(browser))
    if env_bool("REMOTIVE_ENABLED", True):
        scrapers.append(RemotiveScraper())
    if env_bool("HIMALAYAS_ENABLED", True):
        scrapers.append(HimalayasScraper())
    if env_bool("WWR_ENABLED", True):
        scrapers.append(WeWorkRemotelyScraper())
    if env_bool("JOBICY_ENABLED", True):
        scrapers.append(JobicyScraper(geos=env_list("JOBICY_GEOS", "latam,anywhere")))
    if env_bool("WORKINGNOMADS_ENABLED", True):
        scrapers.append(WorkingNomadsScraper())
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
            logger.info("scraper completed", extra={"scraper": scraper.name, "collected": len(jobs)})
            all_jobs.extend(jobs)
        except Exception as exc:  # noqa: BLE001
            logger.error("scraper failed", extra={"scraper": scraper.name, "error": str(exc)})
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
    # Prioritize jobs with positive international/LATAM/Worldwide/contractor signals
    fresh.sort(key=international_fit_score, reverse=True)
    return fresh


def persist(jobs: list[dict], db: Database) -> list[dict]:
    """Insert newly discovered jobs. Detail bodies are fetched lazily at analysis time."""
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


def process_application(
    job: dict,
    analysis: dict,
    *,
    db: Database,
    profile: CandidateProfile,
    resume_generator: TailoredResumeGenerator,
    cover_letter_generator: CoverLetterGenerator,
    storage: ApplicationStorage,
    registry: ApplierRegistry,
    telegram: TelegramClient,
    settings: Settings,
    browser_context=None,
) -> str:
    """Execute document generation, automated submission (or dry-run), DB persistence, and Telegram update."""
    job_id = job["id"]
    chash = job.get("content_hash") or content_hash(job.get("title") or "", job.get("company") or "")
    dry_run = settings.application_dry_run

    # Idempotency guard: never double-apply to the same job or role+company
    dup = db.has_duplicate_application(job_id, chash, include_dry_run=dry_run)
    if dup is not None:
        logger.info(
            "skipping duplicate application",
            extra={"job_id": job_id, "existing_job_id": dup["job_id"], "status": dup["status"]},
        )
        return "duplicate"

    language = (analysis.get("language") or settings.default_language or "pt").lower()[:2]
    existing_app = db.get_application(job_id)

    # Step 1: Prepare tailored ATS resume (reuse if already generated and valid from a prior retry)
    resume_pdf_path = Path(existing_app["custom_resume_path"]) if existing_app and existing_app.get("custom_resume_path") else None
    resume_md_path = storage.resume_md_path(job_id)
    base_resume_key = (existing_app or {}).get("base_resume_key") or analysis.get("resume_key") or ""
    candidate_headline = profile.headline

    need_resume = True
    if resume_pdf_path and resume_pdf_path.exists() and resume_md_path.exists():
        pdf_ok, _ = validate_pdf(resume_pdf_path, min_chars=200)
        if pdf_ok:
            need_resume = False

    if need_resume:
        db.upsert_application(
            job,
            status="preparing_documents",
            match_score=analysis.get("match"),
            language=language,
            base_resume_key=base_resume_key,
            documents_dir=str(storage.dir_for(job_id)),
            max_attempts=settings.application_max_retries,
            dry_run=dry_run,
        )
        try:
            generated = resume_generator.generate(job, analysis)
            resume_pdf_path = generated.pdf_path
            resume_md_path = generated.md_path
            base_resume_key = generated.base_resume_key
            language = generated.language
            candidate_headline = generated.data.get("headline") or candidate_headline
        except Exception as exc:  # noqa: BLE001
            logger.error("resume generation failed", extra={"job_id": job_id, "error": str(exc)})
            app_row, exhausted = db.record_application_failure(
                job_id,
                f"Falha ao gerar currículo personalizado: {exc}",
                retryable=True,
                backoff_minutes=settings.application_retry_backoff_minutes,
            )
            if exhausted:
                telegram.send_alert(format_failure_alert("Falha na geração de currículo", str(exc), job=job))
            return app_row.get("status", "application_failed")

    assert resume_pdf_path is not None
    db.upsert_application(
        job,
        status="documents_ready",
        match_score=analysis.get("match"),
        language=language,
        base_resume_key=base_resume_key,
        custom_resume_path=str(resume_pdf_path),
        documents_dir=str(storage.dir_for(job_id)),
        max_attempts=settings.application_max_retries,
        dry_run=dry_run,
    )

    # Step 2: Lazy cover letter callback — ONLY called if the platform actually requests one
    generated_cover_pdf: list[str] = []

    def cover_letter_provider() -> tuple[str, str]:
        existing_cl = storage.cover_letter_pdf_path(
            job_id,
            candidate_name=profile.full_name,
            language=language,
        )
        existing_cl_md = storage.cover_letter_md_path(job_id)
        if existing_cl.exists() and existing_cl_md.exists():
            ok, _ = validate_pdf(existing_cl, min_chars=150)
            if ok:
                generated_cover_pdf.append(str(existing_cl))
                return str(existing_cl), existing_cl_md.read_text(encoding="utf-8")
        cl = cover_letter_generator.generate(
            job,
            resume_md_path.read_text(encoding="utf-8"),
            language=language,
            candidate_headline=candidate_headline,
        )
        generated_cover_pdf.append(str(cl.pdf_path))
        db.upsert_application(
            job,
            status="documents_ready",
            cover_letter_path=str(cl.pdf_path),
            dry_run=dry_run,
        )
        return str(cl.pdf_path), cl.plain_text

    # Step 3: Attempt automated submission via ApplierRegistry
    db.record_application_attempt(job_id)
    documents = ApplicationDocuments(
        resume_pdf=str(resume_pdf_path),
        resume_md=str(resume_md_path),
        language=language,
    )
    outcome = registry.dispatch(
        job,
        profile,
        documents,
        browser_context=browser_context,
        dry_run=dry_run,
        timeout_seconds=settings.application_timeout_seconds,
        cover_letter_provider=cover_letter_provider,
    )

    cover_path = generated_cover_pdf[0] if generated_cover_pdf else ""
    storage.write_metadata(
        job_id,
        {
            "platform": outcome.platform,
            "apply_url": outcome.apply_url,
            "status": outcome.status,
            "confirmation_id": outcome.confirmation_id,
            "dry_run": outcome.dry_run,
            "cover_letter_requested": outcome.cover_letter_requested,
            "reason": outcome.reason,
            "filled_fields": outcome.filled_fields,
        },
    )

    # Step 4: Persist final outcome & notify Telegram
    if outcome.status == "applied" or (outcome.dry_run and outcome.status == "documents_ready"):
        app_row = db.record_application_success(
            job_id,
            platform=outcome.platform,
            apply_url=outcome.apply_url,
            confirmation_id=outcome.confirmation_id,
            dry_run=outcome.dry_run,
        )
        if cover_path:
            app_row["cover_letter_path"] = cover_path
        msg = format_application_notification(job, analysis, app_row)
        telegram.send_application_bundle(
            msg,
            resume_path=str(resume_pdf_path),
            cover_letter_path=cover_path or None,
            job_slug=sanitize_job_id(job_id),
        )
        db.mark_application_notified(job_id)
        return app_row["status"]

    if outcome.status == "expired":
        db.upsert_application(
            job,
            status="expired",
            platform=outcome.platform,
            apply_url=outcome.apply_url,
            failure_reason=outcome.reason,
            dry_run=dry_run,
        )
        return "expired"

    if outcome.status == "pending_human_action":
        app_row = db.upsert_application(
            job,
            status="pending_human_action",
            platform=outcome.platform,
            apply_url=outcome.apply_url,
            cover_letter_path=cover_path,
            human_action_reason=outcome.reason,
            dry_run=dry_run,
        )
        msg = format_application_notification(job, analysis, app_row)
        telegram.send_application_bundle(
            msg,
            resume_path=str(resume_pdf_path),
            cover_letter_path=cover_path or None,
            job_slug=sanitize_job_id(job_id),
        )
        db.mark_application_notified(job_id)
        return "pending_human_action"

    # application_failed -> controlled retry or critical alert if retries exhausted
    app_row, exhausted = db.record_application_failure(
        job_id,
        outcome.reason or "Falha desconhecida ao enviar candidatura.",
        retryable=outcome.retryable,
        backoff_minutes=settings.application_retry_backoff_minutes,
    )
    if exhausted:
        telegram.send_alert(
            format_failure_alert(
                "Candidatura falhou após retentativas",
                outcome.reason or "Sem detalhes adicionais.",
                job=job,
            )
        )
    return "application_failed"


def run_once(
    *,
    settings: Settings | None = None,
    scrapers_override: list | None = None,
    analyzer_override: JobAnalyzer | None = None,
    registry_override: ApplierRegistry | None = None,
    telegram_override: TelegramClient | None = None,
    browser_context_override=None,
) -> dict:
    """Execute one full resilient cycle and return summary metrics."""
    load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))
    cfg = settings or Settings.from_env()
    setup_logging(cfg.log_level, cfg.log_format)

    try:
        with run_lock(cfg.lock_file):
            return _run_cycle(
                cfg,
                scrapers_override=scrapers_override,
                analyzer_override=analyzer_override,
                registry_override=registry_override,
                telegram_override=telegram_override,
                browser_context_override=browser_context_override,
            )
    except ConcurrencyError as exc:
        logger.warning("skipping cycle due to active lock: %s", exc)
        return {"skipped_lock": True}


def _run_cycle(
    cfg: Settings,
    *,
    scrapers_override=None,
    analyzer_override=None,
    registry_override=None,
    telegram_override=None,
    browser_context_override=None,
) -> dict:
    logger.info(
        "jobbot cycle start",
        extra={
            "auto_apply": cfg.auto_apply_enabled,
            "dry_run": cfg.application_dry_run,
            "model": cfg.agy_model,
            "effort": cfg.agy_reasoning_effort,
        },
    )
    db = Database(cfg.database_path)
    telegram = telegram_override or TelegramClient()
    summary = {
        "collected": 0,
        "fresh": 0,
        "analyzed": 0,
        "rejected": 0,
        "applied": 0,
        "dry_run_ready": 0,
        "pending_human": 0,
        "failed": 0,
        "notified": 0,
    }

    playwright_ctx = None
    browser = None
    context = browser_context_override

    try:
        # 1. Crash recovery for interrupted runs
        recovered = db.recover_interrupted()
        if recovered["applying_escalated"] > 0:
            telegram.send_alert(
                format_failure_alert(
                    "Recuperação pós-reinicialização",
                    f"{recovered['applying_escalated']} candidatura(s) interrompida(s) durante envio foram movidas para pending_human_action.",
                )
            )

        # 2. Load resumes & verified candidate profile (used for profile-driven search + analysis + apply)
        resume_library = ResumeLibrary(cfg.resumes_dir)
        profile = CandidateProfile.load(
            resume_library,
            profile_json_path=cfg.profile_json_path,
            profile_md_path=cfg.profile_md_path,
        )

        # 3. Launch Playwright only if no browser context override was provided
        if context is None and scrapers_override is None:
            from playwright.sync_api import sync_playwright
            from bot.scrapers.browser import launch_options, load_merged_storage_state

            playwright_ctx = sync_playwright().start()
            storage_state = load_merged_storage_state("state")
            browser = playwright_ctx.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            ctx_opts = dict(launch_options())
            if storage_state:
                ctx_opts["storage_state"] = storage_state
            context = browser.new_context(**ctx_opts)

        scrapers = (
            scrapers_override
            if scrapers_override is not None
            else build_scrapers(context, cfg.job_max_age_days, profile=profile)
        )
        by_source = {s.name: s for s in scrapers}

        # 4. Scrape, deduplicate, pre-filter, persist
        all_jobs = collect_jobs(scrapers)
        collected_by = Counter(job.get("source") for job in all_jobs)
        for scraper in scrapers:
            name = scraper.name
            if getattr(scraper, "session_lost", False):
                _alert_once(
                    telegram,
                    f"session_{name}",
                    f"Sessão expirada ({name})",
                    f"O portal {name} está sendo acessado sem autenticação. "
                    f"Reexecute no VPS: `python -m bot.login {name}`",
                )
            elif collected_by.get(name, 0) > 0:
                _clear_alert(f"session_{name}")
        fresh = dedup_and_filter(all_jobs, db, cfg.job_max_age_days)
        summary["collected"] = len(all_jobs)
        summary["fresh"] = len(fresh)
        if fresh:
            persist(fresh, db)

        analyzer = analyzer_override or JobAnalyzer(
            resume_library,
            candidate_profile=profile,
            default_language=cfg.default_language,
        )
        if analyzer.ready:
            _clear_alert("analyzer_down")
        if not analyzer.ready:
            _alert_once(
                telegram,
                "analyzer_down",
                "Analisador IA indisponível",
                "O CLI `agy` não respondeu ou não está no PATH — ciclos seguem sem análise/candidatura até resolver.",
            )
            logger.warning("analyzer unavailable — finishing cycle without AI stages")
            return summary

        # 5. Analyze discovered jobs
        pending = db.jobs_pending_analysis(limit=cfg.max_analyses_per_run)
        for job_row in pending:
            job = normalize(job_row)
            if not job.get("body"):
                scraper = by_source.get(job.get("source"))
                if scraper and hasattr(scraper, "fetch_detail"):
                    try:
                        detail = scraper.fetch_detail(job["url"])
                        job["body"] = detail.get("body", "")
                        if detail.get("expired"):
                            db.set_status(job["id"], "expired", "expired at detail fetch")
                            continue
                        if job["body"]:
                            db.update_job_body(job["id"], job["body"], detail.get("apply_url"))
                            ok_post, post_reason = passes_pre_filters(job)
                            if not ok_post:
                                db.set_status(job["id"], "rejected", post_reason)
                                summary["rejected"] += 1
                                logger.info(
                                    "job filtered after detail fetch",
                                    extra={"job_id": job["id"], "reason": post_reason},
                                )
                                continue
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("detail fetch error", extra={"job_id": job["id"], "error": str(exc)})

            analysis = analyzer.analyze(job)
            if analysis is None:
                db.set_status(job["id"], "analysis_failed", "AI returned invalid/empty response")
                continue

            db.save_analysis(job["id"], analysis)
            summary["analyzed"] += 1

            ok_to_apply, reject_reason = should_apply(analysis, threshold=cfg.job_match_threshold)
            if not ok_to_apply:
                db.set_status(job["id"], "rejected", reject_reason)
                summary["rejected"] += 1
                logger.info("job rejected", extra={"job_id": job["id"], "reason": reject_reason})
            elif not cfg.auto_apply_enabled:
                # Classic recommendation-only mode
                resume_key = analysis.get("resume_key")
                resume_path = str(resume_library.path_for(resume_key)) if resume_key else None
                telegram.send_job(format_job_message(job, analysis), resume_path)
                db.mark_notified(job["id"])
                summary["notified"] += 1

        # 6. Automated application stage
        if cfg.auto_apply_enabled and cfg.max_applications_per_run > 0:
            storage = ApplicationStorage(cfg.applications_dir)
            resume_gen = TailoredResumeGenerator(
                resume_library, profile, storage, default_language=cfg.default_language
            )
            cover_gen = CoverLetterGenerator(profile, storage)
            registry = registry_override or ApplierRegistry()

            today_count = db.count_applications_today()
            remaining_daily = (
                cfg.max_applications_per_run
                if cfg.application_dry_run
                else max(0, cfg.max_applications_per_day - today_count)
            )
            batch_limit = min(cfg.max_applications_per_run, remaining_daily)

            if batch_limit <= 0:
                logger.info(
                    "daily application cap reached",
                    extra={"today": today_count, "max_per_day": cfg.max_applications_per_day},
                )
            else:
                candidates = db.jobs_ready_for_application(limit=batch_limit)
                for idx, job_row in enumerate(candidates):
                    job = normalize(job_row)
                    analysis = db.get_analysis(job["id"])
                    if not analysis:
                        continue
                    ok_to_apply, reject_reason = should_apply(analysis, threshold=cfg.job_match_threshold)
                    if not ok_to_apply:
                        db.set_status(job["id"], "rejected", reject_reason)
                        summary["rejected"] += 1
                        continue

                    result_status = process_application(
                        job,
                        analysis,
                        db=db,
                        profile=profile,
                        resume_generator=resume_gen,
                        cover_letter_generator=cover_gen,
                        storage=storage,
                        registry=registry,
                        telegram=telegram,
                        settings=cfg,
                        browser_context=context,
                    )
                    if result_status == "applied":
                        summary["applied"] += 1
                        summary["notified"] += 1
                    elif result_status == "documents_ready":
                        summary["dry_run_ready"] += 1
                        summary["notified"] += 1
                    elif result_status == "pending_human_action":
                        summary["pending_human"] += 1
                        summary["notified"] += 1
                    elif result_status == "application_failed":
                        summary["failed"] += 1

                    if idx < len(candidates) - 1 and cfg.application_interval_seconds > 0:
                        time.sleep(cfg.application_interval_seconds)

        logger.info("jobbot cycle finished", extra={"summary": summary, "db_stats": db.stats()})
        return summary
    finally:
        db.close()
        if browser is not None:
            browser.close()
        if playwright_ctx is not None:
            playwright_ctx.stop()


if __name__ == "__main__":
    try:
        res = run_once()
    except Exception as exc:  # noqa: BLE001
        load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))
        setup_logging()
        TelegramClient().send_alert(
            format_failure_alert(
                "Ciclo do bot-job falhou",
                f"{type(exc).__name__}: {exc}",
            )
        )
        raise
    sys.exit(0 if res is not None else 1)
