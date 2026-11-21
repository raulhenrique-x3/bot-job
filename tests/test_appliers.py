"""Tests for platform detection, safety guardrails, verified-fact Q&A, Greenhouse API, and registry."""

from __future__ import annotations

import json

import pytest

from bot.appliers import (
    ApplicationDocuments,
    ApplicationOutcome,
    ApplierRegistry,
    FormField,
    HumanInterventionRequired,
    PlatformApplier,
    detect_platform,
)
from bot.appliers.base import (
    detect_captcha,
    detect_mfa_or_auth_wall,
    detect_recommendation_letter_request,
    detect_sensitive_or_legal_field,
    detect_submission_confirmation,
)
from bot.appliers.form_filler import VerifiedFactAnswerer
from bot.appliers.greenhouse import GreenhouseApplier, parse_greenhouse_url
from bot.appliers.lever import parse_lever_url
from bot.profile import CandidateProfile


def test_detect_platform_and_url_parsers():
    assert detect_platform("https://boards.greenhouse.io/acme/jobs/12345") == "greenhouse"
    assert detect_platform("https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555") == "lever"
    assert detect_platform("https://jobs.ashbyhq.com/acme/abc") == "ashby"
    assert detect_platform("https://acme.gupy.io/jobs/98765") == "gupy"
    assert detect_platform("https://www.linkedin.com/jobs/view/123456/") == "linkedin"
    assert detect_platform("https://programathor.com.br/jobs/444") == "programathor"
    assert detect_platform("https://www.geekhunter.com/pt/acme/jobs/dev") == "geekhunter"
    assert detect_platform("mailto:jobs@acme.com") == "email"
    assert detect_platform("https://careers.randomstartup.com/apply") == "generic"

    assert parse_greenhouse_url("https://boards.greenhouse.io/acme/jobs/987654") == ("acme", "987654")
    assert parse_lever_url("https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555") == (
        "acme",
        "11111111-2222-3333-4444-555555555555",
    )


def test_safety_guardrails_detect_captcha_mfa_legal_and_recommendation_letters():
    assert detect_captcha('<div class="g-recaptcha" data-sitekey="xyz"></div>') is not None
    assert detect_captcha('<div class="cf-turnstile"></div>') is not None
    assert detect_captcha("<form><input name='email'></form>") is None

    assert detect_mfa_or_auth_wall("https://www.linkedin.com/checkpoint/challenge") is not None
    assert detect_mfa_or_auth_wall("https://boards.greenhouse.io/acme/jobs/1", "Please sign in to apply") is not None
    assert detect_mfa_or_auth_wall("https://boards.greenhouse.io/acme/jobs/1", "Apply for this job") is None

    assert detect_recommendation_letter_request("Anexe sua carta de recomendação de ex-gestor") is not None
    assert detect_recommendation_letter_request("Upload a recommendation letter") is not None
    assert detect_recommendation_letter_request("Optional cover letter") is None

    assert detect_sensitive_or_legal_field("CPF (obrigatório)") is not None
    assert detect_sensitive_or_legal_field("Voluntary Race / Ethnicity disclosure") is not None
    assert detect_sensitive_or_legal_field("Full Name") is None


def test_submission_confirmation_requires_explicit_signal():
    ok, cid = detect_submission_confirmation("https://boards.greenhouse.io/acme/jobs/1", "Submit application")
    assert ok is False
    assert cid == ""

    ok2, cid2 = detect_submission_confirmation(
        "https://boards.greenhouse.io/acme/jobs/1/confirmation",
        "Thank you for applying! Confirmation #APP-98765",
    )
    assert ok2 is True
    assert cid2 == "APP-98765"

    ok3, cid3 = detect_submission_confirmation(
        "https://example.com/vaga",
        "Candidatura enviada com sucesso! Protocolo: BR202699",
    )
    assert ok3 is True
    assert cid3 == "BR202699"


def test_verified_fact_answerer_deterministic_and_safety(monkeypatch, sample_profile: CandidateProfile):
    answerer = VerifiedFactAnswerer(sample_profile, allow_ai=False)
    job = {"company": "Acme", "title": "Fullstack"}

    assert answerer.answer_field(FormField("first_name", "First Name", "text", True), job=job) == "Raul"
    assert answerer.answer_field(FormField("email", "Email", "email", True), job=job) == "raul@example.com"
    assert answerer.answer_field(FormField("linkedin", "LinkedIn Profile", "url", False), job=job) == "https://linkedin.com/in/raul-henrique"

    # Required sensitive field -> HumanInterventionRequired
    with pytest.raises(HumanInterventionRequired):
        answerer.answer_field(FormField("cpf", "CPF", "text", True), job=job)

    # Recommendation letter -> HumanInterventionRequired
    with pytest.raises(HumanInterventionRequired):
        answerer.answer_field(FormField("ref", "Letter of Recommendation", "file", False), job=job)

    # Non-confident AI response -> HumanInterventionRequired
    monkeypatch.setattr("bot.appliers.form_filler.agy_available", lambda: True)
    monkeypatch.setattr(
        "bot.appliers.form_filler.agy_ask",
        lambda prompt, **kw: json.dumps({"answer": "", "confident": False, "reason": "Pretensão não informada"}),
    )
    ai_answerer = VerifiedFactAnswerer(sample_profile, allow_ai=True)
    with pytest.raises(HumanInterventionRequired, match="Pretensão não informada"):
        ai_answerer.answer_field(FormField("custom_q", "Qual sua pretensão salarial em USD?", "text", True), job=job)


def test_greenhouse_api_inspection_detects_cover_letter_and_recommendation_blocker(monkeypatch):
    applier = GreenhouseApplier()
    monkeypatch.setattr(
        applier,
        "_fetch_board_api",
        lambda token, jid: {
            "title": "Engineer",
            "questions": [
                {"label": "Cover Letter", "required": True, "fields": [{"name": "cover_letter", "type": "input_file"}]},
                {"label": "Full Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
            ],
        },
    )
    insp = applier.inspect({}, "https://boards.greenhouse.io/acme/jobs/123")
    assert insp.cover_letter_requested is True
    assert insp.cover_letter_required is True
    assert insp.blocker_reason == ""

    # Recommendation letter blocks immediately without attempting to apply
    monkeypatch.setattr(
        applier,
        "_fetch_board_api",
        lambda token, jid: {
            "title": "Engineer",
            "questions": [
                {"label": "Upload recommendation letter from manager", "required": True, "fields": []},
            ],
        },
    )
    outcome = applier.apply(
        {"id": "gh:123"},
        "https://boards.greenhouse.io/acme/jobs/123",
        CandidateProfile(),
        ApplicationDocuments(resume_pdf="r.pdf", resume_md="r.md"),
    )
    assert outcome.status == "pending_human_action"
    assert "recomendação" in outcome.reason.lower()


def test_applier_registry_is_extensible():
    class CustomATSApplier(PlatformApplier):
        platform_name = "custom_ats"

        def can_handle(self, apply_url: str, job: dict) -> bool:
            return "customats.dev" in apply_url

        def apply(self, job, apply_url, profile, documents, **kwargs) -> ApplicationOutcome:
            return ApplicationOutcome(status="applied", platform="custom_ats", apply_url=apply_url, confirmation_id="C1")

    registry = ApplierRegistry()
    registry.register(CustomATSApplier())
    selected = registry.select("https://customats.dev/apply/1", {})
    assert selected is not None
    assert selected.platform_name == "custom_ats"


def test_salary_budget_estimator_bounds_and_formatting():
    from bot.appliers.salary import SalaryBudgetEstimator

    profile = CandidateProfile(
        full_name="Raul Henrique",
        salary_min=6000,
        salary_max=16000,
        salary_expectation="R$ 6.000 - R$ 16.000",
    )
    est_engine = SalaryBudgetEstimator(profile, allow_web=False, allow_ai=False)

    senior_pj = est_engine.estimate(
        {"id": "j1", "title": "Senior Python Engineer", "company": "Acme", "seniority": "Senior", "body": ""},
        contract_type="pj",
    )
    pleno_clt = est_engine.estimate(
        {"id": "j2", "title": "Desenvolvedor Pleno", "company": "Acme", "seniority": "Pleno", "body": ""},
        contract_type="clt",
    )
    assert 6000 <= senior_pj.amount_brl <= 16000
    assert 6000 <= pleno_clt.amount_brl <= 16000
    assert senior_pj.amount_brl > pleno_clt.amount_brl

    assert SalaryBudgetEstimator.format_for_field(12500, "Pretensão PJ", "R$ 0,00") == "12500,00"
    assert SalaryBudgetEstimator.format_for_field(14000, "Pretensão Salarial PJ (hora)*", "") == "85"
    assert SalaryBudgetEstimator.format_for_field(10000, "Annual salary", "") == "120000"


def test_load_merged_storage_state(tmp_path):
    from bot.scrapers.browser import load_merged_storage_state

    (tmp_path / "auth_linkedin.json").write_text(
        json.dumps({"cookies": [{"name": "li_at", "domain": ".linkedin.com", "path": "/", "value": "x"}], "origins": []}),
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text(
        json.dumps({"cookies": [{"name": "_pt_sess", "domain": "programathor.com.br", "path": "/", "value": "y"}], "origins": []}),
        encoding="utf-8",
    )
    merged = load_merged_storage_state(str(tmp_path))
    assert merged is not None
    names = {c["name"] for c in merged["cookies"]}
    assert names == {"li_at", "_pt_sess"}


def test_playwright_form_filler_real_browser_multistep_and_submit(tmp_path):
    from playwright.sync_api import sync_playwright

    from bot.appliers.form_filler import PlaywrightFormFiller
    from bot.documents.pdf_renderer import render_resume_pdf

    pdf_path = tmp_path / "resume.pdf"
    render_resume_pdf(
        {
            "full_name": "Raul Henrique",
            "headline": "Fullstack Engineer",
            "contact_line": "Recife, PE | raul@example.com",
            "summary": "Desenvolvedor Fullstack com Python, React e TypeScript.",
            "skills": [{"category": "Stack", "items": ["Python", "React", "TypeScript"]}],
            "experience": [{"role": "Dev", "company": "Acme", "period": "2022 - Atual", "bullets": ["APIs FastAPI"]}],
            "education": [],
            "languages": ["Português", "Inglês (C1)"],
        },
        pdf_path,
        language="pt",
    )
    profile = CandidateProfile(
        full_name="Raul Henrique",
        first_name="Raul",
        last_name="Henrique",
        email="raul@example.com",
        phone="+55 81 98683-8081",
        city="Recife",
        state="PE",
        country="Brasil",
        linkedin_url="https://linkedin.com/in/raul-henrique/",
        github_url="https://github.com/raulhenrique-x3",
        salary_min=6000,
        salary_max=16000,
        has_cnpj=True,
        cnpj_open_days=31,
        availability="Imediata",
        availability_days=0,
        languages=["Português", "Inglês Avançado (C1)"],
    )

    html = """
    <html><body>
      <div id="step1">
        <label for="nome">Nome completo *</label><input id="nome" name="name" required />
        <label for="mail">Email *</label><input id="mail" name="email" type="email" required />
        <button id="nextBtn" type="button" onclick="document.getElementById('step1').style.display='none';document.getElementById('step2').style.display='block';">Próximo</button>
      </div>
      <div id="step2" style="display:none;">
        <label for="pj">Pretensão PJ (hora) *</label><input id="pj" name="salary_pj" required />
        <label for="cv">Currículo *</label><input id="cv" name="resume" type="file" required />
        <label><input id="terms" type="checkbox" required /> Li e aceito a Política de Privacidade</label>
        <button id="submitBtn" type="button" onclick="document.body.innerHTML='<h1>Candidatura enviada com sucesso! Protocolo: TEST-2026</h1>';">Enviar candidatura</button>
      </div>
    </body></html>
    """

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.set_content(html)

        filler = PlaywrightFormFiller(profile, allow_ai_qa=False, allow_salary_web=False)
        outcome = filler.run_on_page(
            page,
            platform="custom_wizard",
            apply_url="https://example.com/apply",
            job={"id": "w1", "title": "Fullstack Pleno", "company": "Acme", "seniority": "Pleno"},
            documents=ApplicationDocuments(resume_pdf=str(pdf_path), resume_md=""),
            dry_run=False,
        )
        browser.close()

    assert outcome.status == "applied"
    assert outcome.confirmation_id == "TEST-2026"
    assert outcome.filled_fields["Nome completo *"] == "Raul Henrique"
    assert outcome.filled_fields["Currículo *"] == "[FILE] resume.pdf"
    # Hourly PJ rate must be converted (~50-100 BRL/h), NOT monthly (e.g. 12000)
    assert int(outcome.filled_fields["Pretensão PJ (hora) *"]) <= 120


def test_confirmation_id_ignores_plain_words():
    from bot.appliers.base import detect_submission_confirmation

    # Nerdin pages contain navigation text like "Cursos Código Testar Software"
    ok, cid = detect_submission_confirmation(
        "https://www.nerdin.com.br/vaga_candidatura.php?id=100820",
        "Cursos Código Testar Software — Sua candidatura foi enviada com sucesso!",
    )
    assert ok is True
    assert cid == ""

    ok2, cid2 = detect_submission_confirmation(
        "https://example.com/thanks",
        "Candidatura enviada! Protocolo: APP-987654",
    )
    assert ok2 is True
    assert cid2 == "APP-987654"

    # Cover letter detection must handle underscore/hyphen field IDs (e.g. Greenhouse id="cover_letter")
    from bot.appliers.base import is_cover_letter_label

    assert is_cover_letter_label("Attach cover_letter") is True
    assert is_cover_letter_label("carta_de_apresentacao") is True


def test_linkedin_2026_sdui_card_parser():
    from bot.scrapers.linkedin import LinkedInScraper

    html = """
    <div componentkey="job-card-component-ref-4491174676" role="button">
      <div>Selecionada, Fullstack Python + React Engineer - Remote Work (vaga verificada)</div>
      <div>BairesDev</div>
      <div>Brasil (Remoto)</div>
    </div>
    """
    scraper = LinkedInScraper(browser=None, search_terms=["python"])
    jobs = scraper._parse_listing(html)
    assert len(jobs) == 1
    assert jobs[0]["id"] == "li:4491174676"
    assert jobs[0]["title"] == "Fullstack Python + React Engineer - Remote Work"
    assert jobs[0]["company"] == "BairesDev"
    assert jobs[0]["remote"] is True


def test_current_salary_is_never_fabricated_and_country_code_maps():
    profile = CandidateProfile(
        full_name="Raul Henrique",
        first_name="Raul",
        last_name="Henrique",
        email="raul@example.com",
        phone="+55 81 98683-8081",
        salary_min=6000,
        salary_max=16000,
    )
    answerer = VerifiedFactAnswerer(profile, allow_ai=False, allow_salary_web=False)

    # Current base salary must NOT be answered with expected salary
    cur_sal = FormField(name="current_sal", label="What is your current base salary? (in reais)", field_type="text", required=True)
    with pytest.raises(HumanInterventionRequired):
        answerer.answer_field(cur_sal, job={"title": "Software Engineer"})

    # Country phone code must select BRA (+55), NOT phone number
    code_fld = FormField(
        name="country_code",
        label="Please select your Country Phone Code",
        field_type="select",
        required=True,
        options=["USA (+1)", "BRA (+55)", "ARG (+54)"],
    )
    assert answerer.answer_field(code_fld, job={}) == "BRA (+55)"


def test_unemployed_status_answered_truthfully_in_en_and_pt():
    profile = CandidateProfile(
        full_name="Raul Henrique",
        first_name="Raul",
        last_name="Henrique",
        currently_employed=False,
        current_company="",
        availability="Imediata",
    )
    answerer = VerifiedFactAnswerer(profile, allow_ai=False, allow_salary_web=False)

    assert answerer.answer_field(
        FormField(
            name="q1",
            label="If you’re currently employed, where are you currently working? (please enter the full company name)*",
            field_type="text",
            required=True,
        ),
        job={"title": "Software Engineer"},
    ) == "Not currently employed"

    assert answerer.answer_field(
        FormField(name="q1b", label="What is your current base salary? (in reais)*", field_type="text", required=True),
        job={"title": "Software Engineer"},
    ) == "N/A (Not currently employed)"

    assert answerer.answer_field(
        FormField(name="q1c", label="Salário atual", field_type="number", required=True),
        job={"title": "Desenvolvedor"},
    ) == "0"

    assert answerer.answer_field(
        FormField(
            name="q_var",
            label="Do you currently receive any variable compensation (e.g., Annual Bonus/PLR, Commissions, Stock Options, RSUs)?*",
            field_type="text",
            required=True,
        ),
        job={"title": "Software Engineer"},
    ) == "No (Not currently employed)"

    assert answerer.answer_field(
        FormField(
            name="q_cit",
            label="Are you a citizen or permanent resident of the country where this position is based?*",
            field_type="select",
            required=True,
            options=["Yes", "No"],
        ),
        job={"title": "Mid-level Software Engineer (Remote - Brazil)", "location": "Brazil"},
    ) == "Yes"

    assert answerer.answer_field(
        FormField(name="q_src", label="How did you hear about this opportunity?*", field_type="text", required=True),
        job={"source": "linkedin", "title": "Engineer"},
    ) == "LinkedIn"

    assert answerer.answer_field(
        FormField(name="q2", label="Onde você trabalha atualmente?", field_type="text", required=True),
        job={"title": "Desenvolvedor Fullstack"},
    ) == "Atualmente desempregado"

    assert answerer.answer_field(
        FormField(name="q3", label="Are you currently employed?", field_type="select", required=True, options=["Yes", "No"]),
        job={},
    ) == "No"

    assert answerer.answer_field(
        FormField(name="q4", label="Você está empregado atualmente?", field_type="select", required=True, options=["Sim", "Não"]),
        job={},
    ) == "Não"

    assert "desempregado" in profile.as_verified_facts_text().lower()


def test_consent_checkbox_id_inside_form_and_disabled_submit_handling(tmp_path):
    from playwright.sync_api import sync_playwright

    from bot.appliers.form_filler import PlaywrightFormFiller
    from bot.documents.pdf_renderer import render_resume_pdf

    pdf_path = tmp_path / "resume.pdf"
    render_resume_pdf(
        {
            "full_name": "Raul Henrique",
            "headline": "Fullstack Engineer",
            "contact_line": "Recife, PE | raul@example.com",
            "summary": "Desenvolvedor Fullstack.",
            "skills": [{"category": "Stack", "items": ["Python"]}],
            "experience": [],
            "education": [],
            "languages": ["Português"],
        },
        pdf_path,
        language="pt",
    )
    profile = CandidateProfile(full_name="Raul Henrique", email="raul@example.com")

    # 1. Form with id="pool-consent" checkbox that enables the submit button (HCODE pattern)
    html_consent = """
    <html><body>
      <form onsubmit="event.preventDefault(); document.body.innerHTML='<h1>Candidatura enviada com sucesso!</h1>';">
        <label for="nome">Nome completo</label><input id="nome" name="name" required />
        <label for="mail">Email</label><input id="mail" name="email" type="email" required />
        <label>
          <input id="pool-consent" type="checkbox" onchange="document.getElementById('btn').disabled = !this.checked;" />
          <span>Li e concordo com o tratamento dos meus dados conforme a LGPD e a Política de Privacidade.</span>
        </label>
        <button id="btn" type="submit" disabled>Enviar candidatura</button>
      </form>
    </body></html>
    """

    # 2. Form whose submit button stays permanently disabled -> pending_human_action (not timeout)
    html_stuck_disabled = """
    <html><body>
      <form>
        <label for="nome">Nome completo</label><input id="nome" name="name" required />
        <label for="mail">Email</label><input id="mail" name="email" type="email" required />
        <button id="btn" type="submit" disabled>Enviar candidatura</button>
      </form>
    </body></html>
    """

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        filler = PlaywrightFormFiller(profile, allow_ai_qa=False, allow_salary_web=False)

        page1 = browser.new_page()
        page1.set_content(html_consent)
        out1 = filler.run_on_page(
            page1,
            platform="generic",
            apply_url="https://hcode.com.br/trabalhe-conosco/dev",
            job={"id": "hc:1", "title": "Dev"},
            documents=ApplicationDocuments(resume_pdf=str(pdf_path), resume_md=""),
            dry_run=False,
        )
        page1.close()

        page2 = browser.new_page()
        page2.set_content(html_stuck_disabled)
        out2 = filler.run_on_page(
            page2,
            platform="generic",
            apply_url="https://example.com/apply",
            job={"id": "hc:2", "title": "Dev"},
            documents=ApplicationDocuments(resume_pdf=str(pdf_path), resume_md=""),
            dry_run=False,
        )
        page2.close()
        browser.close()

    assert out1.status == "applied"
    assert out2.status == "pending_human_action"
    assert "desabilitado" in out2.reason.lower()


def test_portal_expired_detection_and_devagas_removed():
    from bot.appliers.portals import NerdinApplier, ProgramathorApplier
    from bot.main import build_scrapers

    class _FakePage:
        def __init__(self, title: str, body: str):
            self._title = title
            self._body = body

        def title(self):
            return self._title

        def inner_text(self, _sel):
            return self._body

        def locator(self, _sel):
            return self

        def count(self):
            return 1

    nerdin = NerdinApplier()
    assert nerdin._is_expired_page(
        _FakePage("Vaga indisponível para candidatura - Nerdin", "Esta vaga não está aceitando novas candidaturas.")
    ) is True
    assert nerdin._is_expired_page(
        _FakePage("Vaga Dev - Nerdin", "No momento, não estamos aceitando novas candidaturas para esta vaga.")
    ) is True
    assert nerdin._is_expired_page(
        _FakePage("Quero me Candidatar - Nerdin", "Preencha seus dados para se candidatar.")
    ) is False

    pt = ProgramathorApplier()
    assert pt._is_expired_page(_FakePage("Página não disponível (404)", "Erro 404")) is True
    assert pt._is_expired_page(_FakePage("Vaga Dev Full Stack - Programathor", "Quero me candidatar")) is False

    scrapers = build_scrapers(browser=None, max_age_days=14)
    names = [s.name for s in scrapers]
    assert "devagas" not in names
    assert set(names) == {
        "programathor",
        "geekhunter",
        "linkedin",
        "querovagas",
        "nerdin",
        "remotive",
        "himalayas",
        "weworkremotely",
        "jobicy",
        "workingnomads",
        "wellfound",
        "avanade",
    }


def test_linkedin_profile_queries_easy_apply_url_and_mfa_query_param_guard():
    from bot.scrapers.linkedin import LinkedInScraper, build_profile_search_terms

    # Base64 trackingId with %2FA must NOT trigger 2FA auth wall false positive
    url_with_2fa_in_query = (
        "https://www.linkedin.com/jobs/view/4484108587/"
        "?companyName=Avenue+Code&trackingId=tzrGlvZwR9auG%2Fe3o%2FA0Ug%3D%3D"
    )
    assert detect_mfa_or_auth_wall(url_with_2fa_in_query, "Easy Apply") is None
    assert detect_mfa_or_auth_wall("https://www.linkedin.com/checkpoint/challenge", "") is not None

    profile = CandidateProfile(
        headline="Engenheiro de Software | Fullstack & Front-end | React, Next.js, TypeScript, Node.js, Python, IA & LLMs",
        skills=["Python", "TypeScript", "React", "Next.js", "Node.js", "FastAPI"],
    )
    terms = build_profile_search_terms(profile)
    assert len(terms) >= 3
    assert any("React" in t for t in terms)
    assert any("Python" in t for t in terms)

    scraper = LinkedInScraper(browser=None, search_terms=terms, max_days=7, remote_only=True, easy_apply_only=True)
    search_url = scraper._search_url(terms[0], page_n=1)
    assert "f_AL=true" in search_url
    assert "f_WT=2" in search_url

    # English SDUI card with "(Verified job)" duplicate title line must extract real company
    en_html = """
    <div componentkey="job-card-component-ref-4484108587" role="button">
      <div>Full-stack Engineer (Verified job)</div>
      <div>Full-stack Engineer</div>
      <div>Avenue Code</div>
      <div>Brazil (Remote)</div>
    </div>
    """
    parsed = scraper._parse_listing(en_html)
    assert len(parsed) == 1
    assert parsed[0]["title"] == "Full-stack Engineer"
    assert parsed[0]["company"] == "Avenue Code"
    assert parsed[0]["remote"] is True


def test_iter_buttons_scopes_comma_separated_modal_selector():
    from bot.appliers.form_filler import PlaywrightFormFiller

    class _FakeBtn:
        def __init__(self, text: str, btn_type: str = "button"):
            self._text = text
            self._type = btn_type

        def is_visible(self, timeout: int = 300) -> bool:
            return True

        def inner_text(self, timeout: int = 300) -> str:
            return self._text

        def get_attribute(self, name: str):
            return self._type if name == "type" else None

    class _FakeLocator:
        def __init__(self, items):
            self._items = items
            self.scoped_queries: list[str] = []

        @property
        def first(self):
            return self

        def locator(self, css: str):
            self.scoped_queries.append(css)
            return _FakeLocator(self._items)

        def count(self) -> int:
            return len(self._items)

        def nth(self, idx: int):
            return self._items[idx]

    class _FakePage:
        def __init__(self, modal_loc):
            self._modal_loc = modal_loc
            self.root_queries: list[str] = []

        def locator(self, css: str):
            self.root_queries.append(css)
            return self._modal_loc

    submit_btn = _FakeBtn("Submit application")
    modal_loc = _FakeLocator([_FakeBtn("Upload resume"), submit_btn])
    page = _FakePage(modal_loc)
    scope = "dialog[data-testid='dialog'], [data-testid='dialog-content'], .jobs-easy-apply-modal, [role='dialog']"

    found = PlaywrightFormFiller._find_submit_button(page, scope_selector=scope)
    assert found is submit_btn
    assert page.root_queries == [scope]
    assert modal_loc.scoped_queries == ["form button, form input[type='submit'], button[type='submit'], button"]


def test_linkedin_apply_cta_priority_and_sdui_avaliar_radio_wizard(tmp_path):
    from playwright.sync_api import sync_playwright

    from bot.appliers.form_filler import PlaywrightFormFiller
    from bot.appliers.linkedin import LinkedInApplier

    profile = CandidateProfile(
        full_name="Raul Henrique",
        first_name="Raul",
        last_name="Henrique",
        email="raul@example.com",
        phone="+55 81 98683-8081",
        city="Recife",
        state="PE",
        country="Brazil",
        years_of_experience="4+",
        availability="Imediata",
        availability_days=0,
        languages=["Português", "Inglês Avançado (C1)"],
    )

    # 1. Page with misleading links/titles BEFORE the real Easy Apply CTA
    html_job = """
    <html><body>
      <header><a href="/jobs/apply">Apply Jobs</a></header>
      <main>
        <a href="/jobs/view/999/">Applied AI Engineer</a>
        <div componentkey="job-card-component-ref-1"><button>Easy Apply</button></div>
        <a href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fexample.com%2Finfo">Why Apply Through Jobgether?</a>
        <button id="real-easy" type="button" aria-label="Usar a candidatura simplificada para esta vaga">Candidatura simplificada</button>
      </main>
    </body></html>
    """

    # 2. LinkedIn 2026 SDUI multi-step modal: hidden unrelated dialog first, then visible modal with
    #    Avançar -> hidden sr-only radio group + Avaliar -> Enviar candidatura
    html_modal = """
    <html><body>
      <div role="dialog" style="display:none;"><button>Noise</button></div>
      <dialog data-testid="dialog" open style="display:flex; width:800px; height:650px;">
        <div id="s1">
          <label for="fn">First name*</label><input id="fn" type="text" required value="" />
          <label for="ln">Last name*</label><input id="ln" type="text" required value="" />
          <button type="button" onclick="document.getElementById('s1').style.display='none';document.getElementById('s2').style.display='block';">Avançar</button>
        </div>
        <div id="s2" style="display:none;">
          <fieldset aria-describedby="error-message-1" role="radiogroup">
            <div>
              <div><input id="r1" type="radio" name="rg1" aria-label="Are you currently based in Brazil?" style="opacity:0;width:0;height:0;"><label for="r1"></label></div>
              <div><p>Yes</p></div>
            </div>
            <div>
              <div><input id="r2" type="radio" name="rg1" aria-label="Are you currently based in Brazil?" style="opacity:0;width:0;height:0;"><label for="r2"></label></div>
              <div><p>No</p></div>
            </div>
          </fieldset>
          <fieldset aria-describedby="error-message-2" role="radiogroup">
            <div>
              <div><input id="e1" type="radio" name="rg2" aria-label="What is your level of proficiency in English?" style="opacity:0;width:0;height:0;"><label for="e1"></label></div>
              <div><p>None</p></div>
            </div>
            <div>
              <div><input id="e2" type="radio" name="rg2" aria-label="What is your level of proficiency in English?" style="opacity:0;width:0;height:0;"><label for="e2"></label></div>
              <div><p>Professional</p></div>
            </div>
          </fieldset>
          <button type="button" onclick="
            if (!document.getElementById('r1').checked || !document.getElementById('e2').checked) {
              document.getElementById('err').style.display='block';
              return;
            }
            document.getElementById('s2').style.display='none';
            document.getElementById('s3').style.display='block';
          ">Avaliar</button>
          <div id="err" role="alert" style="display:none;">Faça uma seleção</div>
        </div>
        <div id="s3" style="display:none;">
          <p>Revise sua candidatura</p>
          <button type="button" onclick="document.body.innerHTML='<h1>Candidatura enviada</h1>';">Enviar candidatura</button>
        </div>
      </dialog>
    </body></html>
    """

    scope = "dialog[data-testid='dialog'], .jobs-easy-apply-modal, [data-testid='dialog-content'], [role='dialog']"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        p1 = browser.new_page()
        p1.set_content(html_job)
        btn, is_easy = LinkedInApplier._find_apply_button(p1)
        assert btn is not None and is_easy is True
        assert btn.get_attribute("id") == "real-easy"
        p1.close()

        filler = PlaywrightFormFiller(profile, allow_ai_qa=False, allow_salary_web=False)
        p2 = browser.new_page()
        p2.set_content(html_modal)
        out_dry = filler.run_on_page(
            p2,
            platform="linkedin_easy_apply",
            apply_url="https://www.linkedin.com/jobs/view/123/",
            job={"id": "li:123", "title": "Fullstack", "company": "Acme"},
            documents=ApplicationDocuments(resume_pdf="", resume_md=""),
            dry_run=True,
            scope_selector=scope,
            max_steps=8,
        )
        assert out_dry.status == "documents_ready"
        assert out_dry.filled_fields["Are you currently based in Brazil?"] == "Yes"
        assert out_dry.filled_fields["What is your level of proficiency in English?"] == "Professional"
        p2.close()

        p3 = browser.new_page()
        p3.set_content(html_modal)
        out_live = filler.run_on_page(
            p3,
            platform="linkedin_easy_apply",
            apply_url="https://www.linkedin.com/jobs/view/123/",
            job={"id": "li:123", "title": "Fullstack", "company": "Acme"},
            documents=ApplicationDocuments(resume_pdf="", resume_md=""),
            dry_run=False,
            scope_selector=scope,
            max_steps=8,
        )
        assert out_live.status == "applied"
        p3.close()
        browser.close()
