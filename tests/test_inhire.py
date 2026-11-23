"""Tests for InHire ATS applier, public API scraper, URL resolution, and Playwright form automation."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from bot.appliers import ApplicationDocuments, ApplierRegistry, HumanInterventionRequired, detect_platform
from bot.appliers.detector import resolve_apply_url
from bot.appliers.inhire import (
    InHireApplier,
    _prepare_custom_form_answers,
    parse_inhire_url,
)
from bot.documents.pdf_renderer import render_resume_pdf
from bot.profile import CandidateProfile
from bot.scrapers.inhire import InHireScraper, discover_inhire_tenants_from_urls
from bot.scrapers.international import _extract_ats_url


def test_inhire_url_parser_platform_detection_and_discovery():
    u1 = "https://mjv.inhire.app/vagas/d300827d-313b-4cfc-92c1-69a72ccc06ba/arquitetoa-de-solucoes?source=linkedin"
    assert parse_inhire_url(u1) == ("mjv", "d300827d-313b-4cfc-92c1-69a72ccc06ba")

    u2 = "https://acme.inhire.app/tech/vagas/11111111-2222-3333-4444-555555555555/dev-python"
    assert parse_inhire_url(u2) == ("acme", "11111111-2222-3333-4444-555555555555")

    assert parse_inhire_url("https://form-app.inhire.app/vagas/11111111-2222-3333-4444-555555555555") is None
    assert parse_inhire_url("https://mjv.inhire.app/vagas") is None

    assert detect_platform(u1) == "inhire"
    assert detect_platform("https://carreira.inhire.com.br/") == "inhire"
    assert isinstance(ApplierRegistry().select(u1, {"source": "linkedin"}), InHireApplier)

    html_snip = f'<p>Apply at <a href="{u1}">MJV</a></p>'
    assert _extract_ats_url(html_snip) == u1

    tenants = discover_inhire_tenants_from_urls([
        u1,
        u2,
        "https://api.inhire.app/vagas/12345678",
        "https://mjv.inhire.app/vagas/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "https://boards.greenhouse.io/acme/jobs/123",
    ])
    assert tenants == ["mjv", "acme"]

    # Also verify confirmation text patterns added for InHire
    from bot.appliers.base import detect_submission_confirmation

    ok, _ = detect_submission_confirmation(
        u1,
        "Inscrição feita com sucesso! Você se inscreveu na vaga Arquiteto(a) de Soluções.",
    )
    assert ok is True


def test_inhire_scraper_filters_remote_dev_and_fetches_detail(monkeypatch):
    scraper = InHireScraper(tenants=["radix", "acme"], remote_only=True, dev_only=True)

    pages = {
        "radix": {
            "tenantName": "Radix",
            "jobsPage": [
                {
                    "jobId": "60589cd6-d495-48d8-aa8f-05bdbbe70703",
                    "displayName": "Profissional Desenvolvedor FullStack Pleno",
                    "status": "published",
                    "workplaceType": "Remote",
                    "location": "BR",
                    "careerPageId": "default",
                },
                {
                    "jobId": "11111111-1111-1111-1111-111111111111",
                    "displayName": "Analista Fiscal Sênior",
                    "status": "published",
                    "workplaceType": "Remote",
                    "location": "BR",
                    "careerPageId": "default",
                },
                {
                    "jobId": "22222222-2222-2222-2222-222222222222",
                    "displayName": "Software Engineer Python",
                    "status": "published",
                    "workplaceType": "Hybrid",
                    "location": "São Paulo, SP, BR",
                    "careerPageId": "default",
                },
            ],
        },
        "acme": {
            "tenantName": "Acme Tech",
            "jobsPage": [
                {
                    "jobId": "33333333-3333-3333-3333-333333333333",
                    "displayName": "AI Engineer",
                    "status": "published",
                    "workplaceType": "Remote",
                    "location": "BR",
                    "careerPageId": "engenharia",
                },
                {
                    "jobId": "44444444-4444-4444-4444-444444444444",
                    "displayName": "Backend Python",
                    "status": "closed",
                    "workplaceType": "Remote",
                    "location": "BR",
                    "careerPageId": "default",
                },
            ],
        },
    }
    monkeypatch.setattr(scraper, "_fetch_tenant_pages", lambda t: pages.get(t))

    jobs = scraper.scrape()
    assert len(jobs) == 2
    assert jobs[0]["id"] == "ih:radix:60589cd6-d495-48d8-aa8f-05bdbbe70703"
    assert jobs[0]["company"] == "Radix"
    assert jobs[0]["url"] == (
        "https://radix.inhire.app/vagas/60589cd6-d495-48d8-aa8f-05bdbbe70703/"
        "profissional-desenvolvedor-fullstack-pleno"
    )
    assert jobs[1]["id"] == "ih:acme:33333333-3333-3333-3333-333333333333"
    assert jobs[1]["url"] == "https://acme.inhire.app/engenharia/vagas/33333333-3333-3333-3333-333333333333/ai-engineer"

    monkeypatch.setattr(
        scraper,
        "_fetch_job_detail",
        lambda t, jid: (
            200,
            {
                "tenantName": "Radix",
                "status": "published",
                "location": "BR",
                "workplaceType": "Remote",
                "contractType": ["CLT"],
                "description": "<p>Buscamos dev <strong>Python e React</strong>.</p>",
            },
        ),
    )
    detail = scraper.fetch_detail(jobs[0]["url"])
    assert detail["expired"] is False
    assert "Python e React" in detail["body"]
    assert "Modalidade: Remote" in detail["body"]

    monkeypatch.setattr(scraper, "_fetch_job_detail", lambda t, jid: (404, None))
    assert scraper.fetch_detail(jobs[0]["url"]) == {"expired": True, "body": ""}


def test_inhire_applier_inspect_blocks_cpf_handles_expired_and_maps_custom_form(monkeypatch):
    applier = InHireApplier()
    profile = CandidateProfile(
        full_name="Raul Henrique",
        email="raul@example.com",
        languages=["Português", "Inglês Avançado (C1)"],
    )

    # 1. Expired job (404)
    monkeypatch.setattr("bot.appliers.inhire._inhire_get_json", lambda path, tenant: (404, None))
    out_exp = applier.apply(
        {"id": "ih:mjv:1", "title": "Dev"},
        "https://mjv.inhire.app/vagas/d300827d-313b-4cfc-92c1-69a72ccc06ba/dev",
        profile,
        ApplicationDocuments(resume_pdf="r.pdf", resume_md=""),
    )
    assert out_exp.status == "expired"

    # 2. Custom form asking for CPF -> pending_human_action
    def fake_get_cpf(path, tenant):
        if path.startswith("/job-posts/public/pages/"):
            return 200, {"status": "published", "displayName": "Dev"}
        if path.endswith("/subscription"):
            return 200, {"typeformId": "form-cpf"}
        if path.endswith("/forms/public/form/form-cpf"):
            return 200, {
                "fields": [
                    {
                        "id": "fld-cpf",
                        "type": "short_text",
                        "title": "Para seguir precisamos do seu CPF e data de nascimento.",
                        "validations": {"required": True},
                    }
                ]
            }
        return 404, None

    monkeypatch.setattr("bot.appliers.inhire._inhire_get_json", fake_get_cpf)
    out_cpf = applier.apply(
        {"id": "ih:appmax:1", "title": "Dev"},
        "https://appmax.inhire.app/vagas/1150377b-7129-47fa-a6b8-9d3004ef3ec8/dev",
        profile,
        ApplicationDocuments(resume_pdf="r.pdf", resume_md=""),
    )
    assert out_cpf.status == "pending_human_action"
    assert "cpf" in out_cpf.reason.lower()

    # 3. Custom form answer resolution (multiple_choice single & multi + source + english)
    custom_form = {
        "fields": [
            {
                "id": "f-eng",
                "type": "multiple_choice",
                "title": "Qual o seu nível de conversação em inglês?",
                "validations": {"required": True},
                "properties": {
                    "allow_multiple_selection": False,
                    "choices": [
                        {"id": "c-bas", "label": "Básico"},
                        {"id": "c-adv", "label": "Avançado"},
                    ],
                },
            },
            {
                "id": "f-src",
                "type": "multiple_choice",
                "title": "Como você ficou sabendo da vaga?",
                "validations": {"required": True},
                "properties": {
                    "allow_multiple_selection": True,
                    "choices": [
                        {"id": "s-ig", "label": "Instagram"},
                        {"id": "s-li", "label": "LinkedIn"},
                    ],
                },
            },
        ]
    }
    answers, display = _prepare_custom_form_answers(
        custom_form,
        profile,
        job={"id": "ih:1", "source": "linkedin", "title": "Dev"},
        allow_ai=False,
        allow_salary_web=False,
    )
    assert answers == {"f-eng": "c-adv", "f-src": ["s-li"]}
    assert display["[Form] Qual o seu nível de conversação em inglês?"] == "Avançado"

    # Unknown required custom question raises HumanInterventionRequired
    with pytest.raises(HumanInterventionRequired):
        _prepare_custom_form_answers(
            {
                "fields": [
                    {
                        "id": "f-rel",
                        "type": "multiple_choice",
                        "title": "Você possui algum parente trabalhando nesta empresa?",
                        "validations": {"required": True},
                        "properties": {
                            "allow_multiple_selection": False,
                            "choices": [{"id": "r-1", "label": "Sim"}, {"id": "r-2", "label": "Não"}],
                        },
                    }
                ]
            },
            profile,
            job={"id": "ih:1"},
            allow_ai=False,
            allow_salary_web=False,
        )


def test_inhire_applier_playwright_multitab_dropdowns_and_custom_submit(monkeypatch, tmp_path):
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
            "languages": ["Português", "Inglês Avançado (C1)"],
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
        country="Brazil",
        linkedin_url="https://linkedin.com/in/raul-henrique/",
        salary_min=6000,
        salary_max=16000,
        has_cnpj=True,
        languages=["Português", "Inglês Avançado (C1)"],
    )

    inhire_html = """
    <html><head><meta charset="utf-8" /></head><body>
      <a href="https://acme.inhire.app/vagas/60589cd6-d495-48d8-aa8f-05bdbbe70703/fullstack">Vaga</a>
      <form data-component-name="JobForm" method="POST">
        <div id="tab1">
          <div data-component-name="Field"><label for="name">Nome completo*</label><input id="name" name="name" type="text" /></div>
          <div data-component-name="Field"><label for="email">Seu melhor email*</label><input id="email" name="email" type="email" /></div>
          <div data-component-name="Field"><label for="phone">Celular com DDD*</label><input id="phone" name="phone" type="tel" value="+55" /></div>
          <div data-component-name="Field"><label for="linkedinUsername">Linkedin*</label><input id="linkedinUsername" name="linkedinUsername" type="text" /></div>

          <div data-component-name="Dropdown" name="country">
            <div class="react-dropdown-select" onclick="document.getElementById('country-menu').style.display='block'">Selecione o país</div>
            <input name="country" value="" style="opacity:0;width:0px;position:absolute;" />
            <div id="country-menu" style="display:none;">
              <div data-component-name="DropdownOptionsSearch"><input placeholder="Pesquisar" /></div>
              <button type="button" data-component-name="DropdownOption" data-option-value="BR" aria-label="Brasil (BR)"
                      onclick="selectCountry('BR', 'Brasil (BR)')">Brasil (BR)</button>
            </div>
          </div>

          <div id="district-br-wrap" data-component-name="Dropdown" name="districtBr" style="display:none;">
            <div class="react-dropdown-select" onclick="document.getElementById('city-menu').style.display='block'">Informe sua cidade</div>
            <div id="city-menu" style="display:none;">
              <div data-component-name="DropdownOptionsSearch"><input id="city-search" placeholder="Pesquisar" oninput="filterCity(this.value)" /></div>
              <button id="opt-recife" type="button" data-component-name="DropdownOption" data-option-value="Recife - PE" aria-label="Recife - PE"
                      onclick="selectCity('Recife - PE')">Recife - PE</button>
            </div>
          </div>

          <div data-component-name="Field">
            <label for="salaryExpectation">Pretensão salarial como CLT*</label>
            <input id="salaryExpectation" name="salaryExpectation" type="text" />
          </div>

          <div data-component-name="Dropdown" name="contractType">
            <div class="react-dropdown-select" onclick="document.getElementById('ct-menu').style.display='block'">Contrato</div>
            <div id="ct-menu" style="display:none;">
              <button type="button" data-component-name="DropdownOption" aria-label="CLT" onclick="selectContract('CLT')">CLT</button>
              <button type="button" data-component-name="DropdownOption" aria-label="PJ" onclick="selectContract('PJ')">PJ</button>
            </div>
          </div>

          <input type="file" name="resume" onchange="window._hasFile = this.files.length > 0; checkReady();" />
          <label><input type="radio" name="isIndication" value="false" /> Não</label>
          <label><input type="radio" name="isIndication" value="true" /> Sim</label>
          <button id="btn-next" type="button" disabled onclick="goTab2()">Avançar</button>
        </div>

        <div id="tab2" style="display:none;">
          <label><input id="pref-not" type="checkbox" onchange="checkReady()" /> Prefiro não responder</label>
          <label><input id="privacyPolicy" name="privacyPolicy" type="checkbox" onchange="checkReady()" /> Li e concordo com a Política de Privacidade</label>
          <button id="btn-submit" type="submit" disabled onclick="event.preventDefault(); submitApp();">Continuar inscrição</button>
        </div>
      </form>
      <script>
        window._country = '';
        window._city = '';
        window._contract = '';
        window._hasFile = false;
        function selectCountry(v, lbl) {
          window._country = v;
          document.querySelector('[name="country"] input[name="country"]').value = lbl;
          document.getElementById('country-menu').style.display = 'none';
          document.getElementById('district-br-wrap').style.display = 'block';
          checkReady();
        }
        function filterCity(q) {
          document.getElementById('opt-recife').style.display = ('recife - pe'.includes(q.toLowerCase())) ? 'block' : 'none';
        }
        function selectCity(lbl) {
          window._city = lbl;
          document.getElementById('city-menu').style.display = 'none';
          checkReady();
        }
        function selectContract(lbl) {
          window._contract = lbl;
          document.getElementById('ct-menu').style.display = 'none';
          checkReady();
        }
        function checkReady() {
          const ok1 = Boolean(
            document.getElementById('name').value &&
            document.getElementById('email').value &&
            document.getElementById('phone').value.startsWith('+55') &&
            window._country === 'BR' &&
            window._city === 'Recife - PE' &&
            window._contract === 'PJ' &&
            window._hasFile &&
            document.getElementById('salaryExpectation').value.endsWith(',00')
          );
          document.getElementById('btn-next').disabled = !ok1;
          const ok2 = ok1 && document.getElementById('pref-not').checked && document.getElementById('privacyPolicy').checked;
          document.getElementById('btn-submit').disabled = !ok2;
        }
        document.querySelectorAll('#name,#email,#phone,#salaryExpectation').forEach(el => el.addEventListener('input', checkReady));
        function goTab2() {
          document.getElementById('tab1').style.display = 'none';
          document.getElementById('tab2').style.display = 'block';
        }
        async function submitApp() {
          const r = await fetch('https://api.inhire.app/job-talents/public/60589cd6-d495-48d8-aa8f-05bdbbe70703/talents', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: document.getElementById('name').value})
          });
          if (r.ok) {
            document.body.innerHTML = '<h1>Inscrição feita com sucesso!</h1><p>Você se inscreveu na vaga Fullstack.</p>';
          }
        }
      </script>
    </body></html>
    """

    posted_custom: list[tuple[str, str, dict]] = []

    def fake_post(path, tenant, payload):
        posted_custom.append((path, tenant, payload))
        return 200, None

    monkeypatch.setattr("bot.appliers.inhire._inhire_post_json", fake_post)

    applier = InHireApplier()
    docs = ApplicationDocuments(resume_pdf=str(pdf_path), resume_md="")
    meta = {
        "tenant": "acme",
        "job_id": "60589cd6-d495-48d8-aa8f-05bdbbe70703",
        "typeform_id": "tf-123",
    }
    custom_answers = {"f-eng": "c-adv"}
    custom_display = {"[Form] Inglês": "Avançado"}
    job = {"id": "ih:acme:60589cd6-d495-48d8-aa8f-05bdbbe70703", "title": "Fullstack Pleno", "company": "Acme"}
    apply_url = "https://acme.inhire.app/vagas/60589cd6-d495-48d8-aa8f-05bdbbe70703/fullstack"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context()

        # Verify resolve_apply_url extracts InHire link directly from aggregator HTML
        ctx.route(
            "https://aggregator.example/**",
            lambda r: r.fulfill(
                status=200,
                content_type="text/html",
                body='<html><body><a href="https://acme.inhire.app/vagas/60589cd6-d495-48d8-aa8f-05bdbbe70703/fullstack">Candidatar</a></body></html>',
            ),
        )
        resolved_url, resolved_plat = resolve_apply_url(
            {"url": "https://aggregator.example/job/1", "source": "querovagas"},
            browser_context=ctx,
        )
        assert resolved_url == apply_url
        assert resolved_plat == "inhire"

        # 1. Dry run
        p_dry = ctx.new_page()
        p_dry.route("https://acme.inhire.app/**", lambda r: r.fulfill(status=200, content_type="text/html; charset=utf-8", body=inhire_html))
        p_dry.goto(apply_url)
        out_dry = applier._apply_on_page(
            p_dry,
            job=job,
            apply_url=apply_url,
            profile=profile,
            documents=docs,
            meta=meta,
            custom_answers=custom_answers,
            custom_display_fields=custom_display,
            cover_letter_requested=False,
            cover_letter_provider=None,
            dry_run=True,
            timeout_seconds=15,
            allow_ai=False,
            allow_salary_web=False,
        )
        assert out_dry.status == "documents_ready", f"reason={out_dry.reason} fields={out_dry.filled_fields}"
        assert out_dry.filled_fields["País"] == "Brasil (BR)"
        assert out_dry.filled_fields["Cidade"] == "Recife - PE"
        assert out_dry.filled_fields["Tipo de contrato"] == "PJ"
        assert out_dry.filled_fields["Telefone"] == "+5581986838081"
        assert out_dry.filled_fields["Grupo de diversidade"] == "Prefiro não responder"
        assert out_dry.filled_fields["Política de Privacidade"] == "true"
        assert posted_custom == []
        p_dry.close()

        # 2. Live run with Stage 1 + Stage 2 submission
        p_live = ctx.new_page()
        p_live.route("https://acme.inhire.app/**", lambda r: r.fulfill(status=200, content_type="text/html; charset=utf-8", body=inhire_html))
        p_live.route(
            "https://api.inhire.app/job-talents/public/**",
            lambda r: r.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"talentId": "tal-uuid-999"}),
            ),
        )
        p_live.goto(apply_url)
        out_live = applier._apply_on_page(
            p_live,
            job=job,
            apply_url=apply_url,
            profile=profile,
            documents=docs,
            meta=meta,
            custom_answers=custom_answers,
            custom_display_fields=custom_display,
            cover_letter_requested=False,
            cover_letter_provider=None,
            dry_run=False,
            timeout_seconds=15,
            allow_ai=False,
            allow_salary_web=False,
        )
        assert out_live.status == "applied", f"reason={out_live.reason} fields={out_live.filled_fields}"
        assert out_live.confirmation_id == "tal-uuid-999"
        assert len(posted_custom) == 1
        assert posted_custom[0] == (
            "/forms/form/submit",
            "acme",
            {
                "talentId": "tal-uuid-999",
                "formId": "tf-123",
                "jobId": "60589cd6-d495-48d8-aa8f-05bdbbe70703",
                "type": "subscription",
                "answers": {"f-eng": "c-adv"},
            },
        )
        p_live.close()
        browser.close()
