"""Tests for Avanade scraper, foreign Remote+LATAM filter, Java 1y experience Q&A, Workday applier, and scraper alerts."""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from bot.appliers import ApplicationDocuments, ApplierRegistry, FormField, detect_platform
from bot.appliers.avanade import AvanadeApplier
from bot.appliers.form_filler import VerifiedFactAnswerer
from bot.documents.pdf_renderer import render_resume_pdf
from bot.login import TARGETS
from bot.main import run_once
from bot.profile import CandidateProfile
from bot.scrapers.avanade import (
    DEFAULT_AVANADE_URL,
    AvanadeScraper,
    is_eligible_avanade_job,
    parse_avanade_search_url,
)
from bot.scrapers.base import JobSource


def test_parse_avanade_search_url_and_platform_registration():
    parsed = parse_avanade_search_url(DEFAULT_AVANADE_URL)
    assert parsed["areas"] == ["Software Engineering AV", "AI & Data"]
    assert parsed["sort_criteria"] == "@job_area_of_expertise ascending"

    assert "avanade" in TARGETS
    assert TARGETS["avanade"]["state"] == "state/auth_avanade.json"

    wd_url = "https://accenture.wd103.myworkdayjobs.com/AvanadeCareers/job/Pernambuco---Recife/Dev_R00364809/apply"
    assert detect_platform(wd_url) == "avanade"
    assert detect_platform("https://www.avanade.com/en/career/search-jobs") == "avanade"
    assert isinstance(ApplierRegistry().select(wd_url, {"source": "avanade"}), AvanadeApplier)


def test_avanade_foreign_remote_latam_filter():
    # Brazil roles are eligible
    assert is_eligible_avanade_job(
        country="Brazil", location="Recife, Brazil", title="Desenvolvedor FullStack", body="Híbrido Recife"
    )[0] is True

    # Foreign non-remote -> blocked
    ok, reason = is_eligible_avanade_job(
        country="United States", location="Chicago, United States", title="Software Engineer", body="On-site role"
    )
    assert ok is False and "not remote" in reason

    # Foreign remote without LATAM -> blocked
    ok, reason = is_eligible_avanade_job(
        country="United Kingdom", location="Manchester, United Kingdom", title="Cloud Architect", body="Remote UK role"
    )
    assert ok is False and "LATAM" in reason

    # Foreign mentioning LATAM but Workday remoteType=Hybrid -> blocked
    ok, reason = is_eligible_avanade_job(
        country="United States",
        location="Miami, United States",
        title="Backend Engineer",
        body="Remote across LATAM",
        remote_type="Hybrid",
    )
    assert ok is False and "non-remote" in reason

    # Foreign with US-only restriction -> blocked even if mentioning remote/LATAM
    ok, reason = is_eligible_avanade_job(
        country="United States",
        location="Remote",
        title="Software Engineer",
        body="Remote LATAM collaboration, US citizens only, no visa sponsorship.",
    )
    assert ok is False

    # Foreign "anywhere in the United States" must be blocked (NOT treated as LATAM/Worldwide)
    ok, reason = is_eligible_avanade_job(
        country="United States",
        location="Chicago, United States",
        title="Azure Enterprise Architect",
        body="This position is open to candidates located anywhere in the United States. Work remotely.",
    )
    assert ok is False

    # Foreign Remote + LATAM -> allowed
    ok, reason = is_eligible_avanade_job(
        country="United States",
        location="Remote - LATAM",
        title="Senior Software Engineer",
        body="100% Remote position open to candidates across Latin America (LATAM).",
        remote_type="Remote",
    )
    assert ok is True and reason == ""


def test_avanade_scraper_parses_coveo_and_filters_foreign():
    scraper = AvanadeScraper(verify_workday_meta=False)
    sample_results = [
        {
            "title": "Consultor(a) Back End | Spring Boot",
            "raw": {
                "job_id": "R00361772",
                "job_name": "Consultor(a) Back End | Spring Boot",
                "jobcountry": "Brazil",
                "jobcityname": "Recife",
                "jobareaofexpertise": "Software Engineering AV",
                "skill_name": "Java",
                "jobposteddate": 1794614400000,
                "jobdescription": "<p>Desenvolvimento Java e Spring Boot em Recife.</p>",
                "qualification": "<ul><li>Experiência com Java</li></ul>",
                "apply_url": "https://accenture.wd103.myworkdayjobs.com/AvanadeCareers/job/Recife/Back-End_R00361772/apply",
            },
        },
        {
            "title": "Azure Data Engineer",
            "raw": {
                "job_id": "R00368779",
                "job_name": "Azure Data Engineer",
                "jobcountry": "United States",
                "jobcityname": "Chicago",
                "jobareaofexpertise": "AI & Data",
                "jobdescription": "<p>Chicago IL role.</p>",
                "apply_url": "https://accenture.wd103.myworkdayjobs.com/AvanadeCareers/job/Chicago/Azure_R00368779/apply",
            },
        },
        {
            "title": "AI Engineer (Remote LATAM)",
            "raw": {
                "job_id": "R00999999",
                "job_name": "AI Engineer (Remote LATAM)",
                "jobcountry": "United States",
                "jobcityname": "Remote",
                "jobareaofexpertise": "AI & Data",
                "jobdescription": "<p>Remote role open for LATAM engineers.</p>",
                "apply_url": "https://accenture.wd103.myworkdayjobs.com/AvanadeCareers/job/Remote/AI_R00999999/apply",
            },
        },
    ]

    jobs = scraper._parse_results(sample_results)
    assert [j["id"] for j in jobs] == ["av:R00361772", "av:R00999999"]
    assert jobs[0]["url"].endswith("/Back-End_R00361772")
    assert jobs[0]["apply_url"].endswith("/Back-End_R00361772/apply")
    assert jobs[0]["company"] == "Avanade"
    assert "Java" in jobs[0]["body"]


def test_verified_fact_answerer_java_one_year_and_workday_fields():
    profile = CandidateProfile(
        full_name="Raul Henrique",
        first_name="Raul",
        last_name="Henrique",
        email="raul@example.com",
        phone="+55 81 98683-8081",
        country="Brazil",
        skills=["Python", "TypeScript", "JavaScript", "Java"],
        skill_experience_years={"Java": "1"},
    )
    answerer = VerifiedFactAnswerer(profile, allow_ai=False, allow_salary_web=False)
    job = {"source": "avanade", "company": "Avanade", "title": "Backend Java"}

    assert answerer.answer_field(
        FormField("q_java", "How many years of experience do you have with Java?", "number", True), job=job
    ) == "1"
    assert answerer.answer_field(
        FormField("q_java_pt", "Quantos anos de experiência com Java?", "text", True), job=job
    ) == "1"
    assert answerer.answer_field(
        FormField(
            "q_java_dd",
            "Years of experience with Java *",
            "select",
            True,
            options=["None", "1-2 years", "3-5 years", "5+ years"],
        ),
        job=job,
    ) == "1-2 years"
    assert answerer.answer_field(
        FormField("q_java_bool", "Do you have experience with Java?", "select", True, options=["Yes", "No"]),
        job=job,
    ) == "Yes"
    # Must NOT confuse JavaScript with Java
    assert answerer.answer_field(
        FormField("q_js", "How many years of experience with JavaScript?", "text", False), job=job
    ) is None

    # Workday-standard questions
    assert answerer.answer_field(
        FormField("prev", "Have you previously worked for Avanade or Accenture?", "select", True, options=["Yes", "No"]),
        job=job,
    ) == "No"
    assert answerer.answer_field(
        FormField("dev_type", "Phone Device Type *", "select", True, options=["Home", "Mobile", "Work"]),
        job=job,
    ) == "Mobile"
    assert answerer.answer_field(
        FormField(
            "source",
            "How Did You Hear About Us? *",
            "select",
            True,
            options=["LinkedIn", "Avanade Career Site", "Other"],
        ),
        job=job,
    ) == "Avanade Career Site"


def test_avanade_applier_auth_wall_and_authenticated_multistep_flow(tmp_path):
    pdf_path = tmp_path / "resume.pdf"
    render_resume_pdf(
        {
            "full_name": "Raul Henrique",
            "headline": "Software Engineer",
            "contact_line": "Recife, PE | raul@example.com",
            "summary": "Fullstack engineer with Python, React, TypeScript and 1 year of Java.",
            "skills": [{"category": "Backend", "items": ["Python", "Node.js", "Java"]}],
            "experience": [{"role": "Dev", "company": "BSATech", "period": "2022 - 2025", "bullets": ["APIs"]}],
            "education": [],
            "languages": ["Portuguese", "English C1"],
        },
        pdf_path,
        language="en",
    )
    profile = CandidateProfile(
        full_name="Raul Henrique",
        first_name="Raul",
        last_name="Henrique",
        email="raul@example.com",
        phone="+55 81 98683-8081",
        city="Recife",
        state="PE",
        country="Brazil",
        linkedin_url="https://linkedin.com/in/raul-henrique/",
        skills=["Python", "Java"],
        skill_experience_years={"Java": "1"},
    )
    docs = ApplicationDocuments(resume_pdf=str(pdf_path), resume_md="")
    job = {"id": "av:R1", "source": "avanade", "company": "Avanade", "title": "Java Backend Dev"}
    applier = AvanadeApplier()

    auth_wall_html = """
    <html><body>
      <div data-automation-id="progressBarActiveStep">Create Account/Sign In</div>
      <button data-automation-id="GoogleSignInButton">Sign in with Google</button>
      <button data-automation-id="SignInWithEmailButton">Sign in with email</button>
      <input name="website" data-automation-id="beecatcher" />
    </body></html>
    """

    wizard_html = """
    <html><body>
      <div id="stepLabel" data-automation-id="progressBarActiveStep">My Information</div>
      <div id="s1">
        <div data-automation-id="formField-source">
          <label id="lbl-src">How Did You Hear About Us? *</label>
          <button id="dd-src" type="button" aria-haspopup="listbox" aria-labelledby="lbl-src" aria-required="true"
                  onclick="document.getElementById('lb-src').style.display='block'">Select One</button>
          <div id="lb-src" role="listbox" style="display:none;">
            <div role="option" onclick="pick('dd-src','lb-src','Avanade Career Site')">Avanade Career Site</div>
            <div role="option" onclick="pick('dd-src','lb-src','LinkedIn')">LinkedIn</div>
          </div>
        </div>
        <label for="fname">First Name *</label><input id="fname" name="first_name" required />
        <label for="lname">Last Name *</label><input id="lname" name="last_name" required />
        <label for="cv">Resume *</label><input id="cv" data-automation-id="file-upload-input-ref" type="file" required />
      </div>
      <div id="s2" style="display:none;">
        <label for="java_yrs">How many years of experience do you have with Java? *</label>
        <input id="java_yrs" name="java_years" type="number" required />
        <label><input id="agree" data-automation-id="agreementCheckbox" type="checkbox" required /> I accept the Privacy Policy</label>
      </div>
      <div id="s3" style="display:none;">
        <p>Review your application before submitting.</p>
      </div>
      <button id="nextBtn" data-automation-id="bottom-navigation-next-button" type="button" onclick="advance()">Save and Continue</button>
      <script>
        let step = 1;
        function pick(btnId, lbId, val) {
          document.getElementById(btnId).innerText = val;
          document.getElementById(lbId).style.display = 'none';
        }
        function advance() {
          if (step === 1) {
            document.getElementById('s1').style.display = 'none';
            document.getElementById('s2').style.display = 'block';
            document.getElementById('stepLabel').innerText = 'Application Questions';
            step = 2;
          } else if (step === 2) {
            document.getElementById('s2').style.display = 'none';
            document.getElementById('s3').style.display = 'block';
            document.getElementById('stepLabel').innerText = 'Review';
            document.getElementById('nextBtn').innerText = 'Submit';
            step = 3;
          } else {
            document.body.innerHTML = '<h1>Thank you for applying! Confirmation #AV-202699</h1>';
          }
        }
      </script>
    </body></html>
    """

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])

        # 1. Auth wall must cleanly escalate to pending_human_action
        p1 = browser.new_page()
        p1.set_content(auth_wall_html)
        auth_out = applier._run_workday_flow(
            p1, job=job, apply_url="https://accenture.wd103.myworkdayjobs.com/AvanadeCareers/job/x/apply",
            profile=profile, documents=docs, dry_run=True, timeout_seconds=15, cover_letter_provider=None,
        )
        assert auth_out.status == "pending_human_action"
        assert "bot.login avanade" in auth_out.reason
        p1.close()

        # 2. Authenticated dry-run walks steps, fills dropdown + Java=1, stops before Submit
        p2 = browser.new_page()
        p2.set_content(wizard_html)
        dry_out = applier._run_workday_flow(
            p2, job=job, apply_url="https://accenture.wd103.myworkdayjobs.com/AvanadeCareers/job/x/apply",
            profile=profile, documents=docs, dry_run=True, timeout_seconds=15, cover_letter_provider=None,
        )
        assert dry_out.status == "documents_ready"
        assert dry_out.filled_fields["First Name *"] == "Raul"
        assert dry_out.filled_fields["How Did You Hear About Us? *"] == "Avanade Career Site"
        assert dry_out.filled_fields["How many years of experience do you have with Java? *"] == "1"
        assert "Review" in dry_out.reason
        p2.close()

        # 3. Authenticated live run submits and captures confirmation ID
        p3 = browser.new_page()
        p3.set_content(wizard_html)
        live_out = applier._run_workday_flow(
            p3, job=job, apply_url="https://accenture.wd103.myworkdayjobs.com/AvanadeCareers/job/x/apply",
            profile=profile, documents=docs, dry_run=False, timeout_seconds=15, cover_letter_provider=None,
        )
        assert live_out.status == "applied"
        assert live_out.confirmation_id == "AV-202699"
        p3.close()

        browser.close()


def test_scraper_failure_sends_telegram_alert(monkeypatch, tmp_path, test_settings):
    from bot import main as bot_main

    monkeypatch.setattr(bot_main, "_alert_marker_dir", lambda: tmp_path / "alerts")
    monkeypatch.setattr("bot.gemini.analysis.agy_available", lambda: False)

    class _BrokenScraper(JobSource):
        name = "avanade"

        def scrape(self) -> list[dict]:
            raise RuntimeError("Coveo API HTTP 503")

    class _FakeTG:
        def __init__(self):
            self.alerts: list[str] = []

        def send_alert(self, msg: str) -> bool:
            self.alerts.append(msg)
            return True

    tg = _FakeTG()
    run_once(
        settings=test_settings,
        scrapers_override=[_BrokenScraper()],
        telegram_override=tg,
    )
    assert any("Falha no scraper (avanade)" in a and "503" in a for a in tg.alerts)
