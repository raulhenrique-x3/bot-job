# bot-job

> [🇧🇷 Português](README.md) | 🇺🇸 **English (en-US)**

Automated agent for job discovery, AI-powered matching, ATS-friendly document generation, and job application automation (Brazilian & international remote roles) with Telegram notifications, powered by **Gemini 4 Neon (Medium)** via the `agy` CLI.

![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## How It Works

```
┌────────────┐   ┌───────────┐   ┌──────────┐   ┌────────────┐   ┌────────────┐   ┌──────────┐
│  Scrapers  │ → │  Filters  │ → │    AI    │ → │ Documents  │ → │  Appliers  │ → │ Telegram │
│ 10 portals │   │ Geo/Dedupe│   │  (Neon)  │   │ ATS (PDF)  │   │ (API/Web)  │   │ + PDFs   │
└────────────┘   └───────────┘   └──────────┘   └────────────┘   └────────────┘   └──────────┘
```

1. **Discovery (10 job portals)**
   - **Brazilian / Playwright & API (5):** Programathor, GeekHunter, **LinkedIn** (profile-driven search with *Easy Apply* and remote filters), QueroVagas Tech, and Nerdin.
   - **International Remote via public JSON/RSS APIs (5):** Remotive, Himalayas, We Work Remotely, Jobicy, and Working Nomads — no headless browser required during discovery.
2. **Pre-filtering, Geo-filtering & Deduplication**
   - Filters by maximum posting age, blocked title keywords/companies, blocked seniority levels, and deduplicates by job ID + SHA-1 (`title|company`).
   - **International geo-filter:** blocks country-restricted listings (`US only`, `W2 only`, `no visa sponsorship`, `UK/EU/Canada only`, etc.) and prioritizes roles open to **LATAM / Worldwide / Anywhere / Contractor / B2B**.
   - Re-validates pre-filters after fetching full job descriptions and uses a balanced round-robin analysis queue across portals (prioritizing LinkedIn).
3. **AI-Powered Matching (`gemini-4-neon-medium`, effort `medium`)**
   - Evaluates technical fit, real experience, seniority, required/nice-to-have stack, hard blockers, language (`pt`/`en`), work mode/location, and compensation.
   - Incompatible roles or roles with hard blockers are marked `rejected` without wasting document generation.
4. **Tailored ATS Document Generation**
   - Picks the best-matching base resume in `resumes/`, generates `resume.md` and `resume.pdf` in the job's language (single-column, no tables/columns/icons, validated via `pypdf` and factual/date consistency checks).
   - Generates `cover_letter.md` / `cover_letter.pdf` **only** when the platform asks for a cover letter.
5. **Extensible Application Automation (`bot/appliers/`)**
   - Resolves outbound aggregator links to detect the underlying ATS (`boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, etc.).
   - Prioritizes official APIs (**Greenhouse Job Board API**, **Lever Postings API**) and Playwright web automation (**Ashby**, **LinkedIn Easy Apply**, **Gupy**, **InHire**, **Programathor**, **GeekHunter**, **Nerdin**, and generic web forms).
   - Automatically detects closed/404 job pages (`expired`) and requires explicit platform confirmation before marking a job as `applied`.
6. **Safety & Human-in-the-Loop Escalation**
   - CAPTCHAs, MFA/login walls, sensitive/legal declarations, unverified custom questions, or third-party recommendation letter requests are **never** bypassed or fabricated; the job is moved to `pending_human_action` with a Telegram notification containing the direct URL and ready-to-upload PDFs.
7. **State Management & Reliability (`data/jobs.db`)**
   - Full lifecycle tracking (`discovered → analyzed → rejected | preparing_documents → documents_ready → applying → applied | pending_human_action | application_failed | expired`), duplicate-application idempotency, backoff retries, post-crash recovery, and `flock` concurrency locking.
8. **Dry-Run Mode (`APPLICATION_DRY_RUN=true`)**
   - Runs discovery, AI analysis, PDF generation/validation, and form filling **without clicking Submit**.

## Project Structure

```
bot/
├── main.py                  # orchestration with lock, crash recovery, and rate limits
├── config.py                # typed Settings, structured JSON logs, secret redaction, run_lock
├── profile.py               # ResumeLibrary (pypdf normalization) + CandidateProfile
├── generate_profile.py      # builds state/profile.md + state/profile.json
├── login.py                 # interactive Playwright login saving sessions with 0600 permissions
├── scrapers/
│   ├── linkedin.py          # profile-driven search + Easy Apply
│   ├── programathor.py      # Programathor
│   ├── geekhunter.py        # GeekHunter
│   ├── querovagas.py        # QueroVagas Tech
│   ├── nerdin.py            # Nerdin
│   └── international.py     # Remotive, Himalayas, We Work Remotely, Jobicy, Working Nomads
├── filters/filters.py       # content_hash, LATAM/Worldwide geo-filter, pre-filters, detect_language
├── gemini/
│   ├── client.py            # unified agy CLI client (Gemini 4 Neon, Medium, retries)
│   ├── prompts.py           # analysis, ATS resume, cover letter, and verified Q&A prompts
│   └── analysis.py          # JobAnalyzer + should_apply
├── documents/
│   ├── storage.py           # data/applications/{job_id}/ (resume.*, cover_letter.*, metadata.json)
│   ├── pdf_renderer.py      # ReportLab single-column ATS renderer + pypdf validator
│   ├── resume_generator.py  # TailoredResumeGenerator + factual consistency checks
│   └── cover_letter.py      # on-demand CoverLetterGenerator
├── appliers/
│   ├── base.py              # contracts + CAPTCHA, MFA, legal, and confirmation detectors
│   ├── detector.py          # platform detection & ATS URL resolution (Greenhouse/Lever/Ashby)
│   ├── form_filler.py       # VerifiedFactAnswerer + PlaywrightFormFiller
│   ├── salary.py            # consistent salary expectation handling
│   ├── greenhouse.py        # Greenhouse Job Board API + Playwright
│   ├── lever.py             # Lever Postings API + Playwright
│   ├── ashby.py / gupy.py / inhire.py / linkedin.py / portals.py / generic.py
│   └── registry.py          # extensible ApplierRegistry
├── database/db.py           # SQLite (jobs, applications, application_events) + round-robin queue
└── telegram/
    ├── client.py            # messages and multi-PDF attachments (stdlib urllib)
    └── format.py            # applied, human-action, dry-run, and alert notifications
deploy/                      # systemd service/timer (every 30 min) + failure alerts
tests/                       # pytest suite (unit + end-to-end integration tests)
```

## Quick Start

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cp .env.example .env         # configure TELEGRAM_* and keep APPLICATION_DRY_RUN=true while testing
```

Place your base resumes in `resumes/` (git-ignored) and generate your consolidated candidate profile:

```bash
.venv/bin/python -m bot.generate_profile   # creates state/profile.md and state/profile.json
```

Save authenticated browser sessions where needed:

```bash
.venv/bin/python -m bot.login linkedin
.venv/bin/python -m bot.login programathor
```

Run a full pipeline cycle (defaults to `APPLICATION_DRY_RUN=true` for safe validation):

```bash
.venv/bin/python -m bot.main
```

Run the test suite:

```bash
.venv/bin/pytest -v
```

Ready-to-use **Docker** (`Dockerfile` / `compose.yaml`) and Linux **systemd** (`deploy/jobbot.timer` + `deploy/jobbot.service`) configurations are also included.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Configuration & Environment Variables](docs/CONFIGURATION.md)
- [Installation & Deployment](docs/INSTALL.md)

## License

MIT
