"""Unit tests for international remote job scrapers, geo pre-filters, and ATS URL detection."""

from __future__ import annotations

from bot.appliers.detector import detect_platform
from bot.filters.filters import international_fit_score, passes_pre_filters
from bot.scrapers.international import (
    HimalayasScraper,
    JobicyScraper,
    RemotiveScraper,
    WeWorkRemotelyScraper,
    WorkingNomadsScraper,
)


def test_international_filter_blocks_us_only_and_w2_phrases():
    cases = [
        {"title": "Senior Backend Engineer", "location": "Remote", "body": "Must be authorized to work in the US without sponsorship."},
        {"title": "Fullstack Engineer (US Only)", "location": "Remote", "body": "React + Node.js"},
        {"title": "Python Developer", "location": "Remote", "body": "Contract W2 only. No visa sponsorship."},
        {"title": "Frontend Engineer", "location": "USA", "body": "Great remote team."},
        {"title": "Backend Engineer", "location": "Europe", "body": "Remote role."},
        {"title": "Fullstack Engineer", "location": "Remote", "body": "Candidates must reside in the United States."},
    ]
    for job in cases:
        ok, reason = passes_pre_filters(job, blocked_title_keywords=[])
        assert not ok, f"Expected blocked for {job}, got ok=True"
        assert reason


def test_international_filter_allows_latam_worldwide_and_scores_positives():
    open_jobs = [
        {"title": "Fullstack Engineer", "location": "Worldwide", "body": "Remote B2B contractor role for LATAM and Americas."},
        {"title": "Python Engineer", "location": "APAC, EMEA, LATAM, USA", "body": "Remote anywhere in the world."},
        {"title": "React Developer", "location": "Anywhere in the World", "body": "Async remote team."},
        {"title": "Desenvolvedor Fullstack", "location": "Remoto - SP", "body": "Vaga CLT ou PJ no Brasil."},
    ]
    for job in open_jobs:
        ok, reason = passes_pre_filters(job, blocked_title_keywords=[])
        assert ok, f"Expected allowed for {job}, got blocked: {reason}"

    assert international_fit_score(open_jobs[0]) > international_fit_score(open_jobs[3])


def test_remotive_parser_extracts_ats_url_and_filters_non_dev():
    scraper = RemotiveScraper()
    items = [
        {
            "id": 101,
            "url": "https://remotive.com/remote-jobs/software-dev/senior-fullstack-101",
            "title": "Senior Fullstack Engineer",
            "company_name": "Acme",
            "category": "Software Development",
            "tags": ["react", "python"],
            "job_type": "full_time",
            "publication_date": "2026-11-19T12:00:00",
            "candidate_required_location": "Worldwide",
            "salary": "$90k - $120k",
            "description": '<p>Apply at <a href="https://boards.greenhouse.io/acme/jobs/12345">Greenhouse</a></p>',
        },
        {
            "id": 102,
            "url": "https://remotive.com/remote-jobs/all-others/content-reviewer-102",
            "title": "Content Reviewer",
            "company_name": "OtherCo",
            "category": "All others",
            "tags": ["teaching"],
            "candidate_required_location": "Worldwide",
            "description": "<p>Review content</p>",
        },
    ]
    jobs = scraper._parse_items(items)
    assert len(jobs) == 1
    assert jobs[0]["id"] == "rm:101"
    assert jobs[0]["apply_url"] == "https://boards.greenhouse.io/acme/jobs/12345"
    assert detect_platform(jobs[0]["apply_url"], "remotive") == "greenhouse"


def test_himalayas_parser_converts_timestamp_and_filters_finance():
    scraper = HimalayasScraper()
    items = [
        {
            "title": "Staff Frontend Engineer",
            "companyName": "StartupX",
            "parentCategories": ["Software Engineering"],
            "categories": ["Frontend-Developer"],
            "locationRestrictions": [],
            "timezoneRestrictions": [-5, -4, -3],
            "minSalary": 100000,
            "maxSalary": 140000,
            "currency": "USD",
            "salaryPeriod": "annual",
            "seniority": ["Senior"],
            "pubDate": 1795200000,
            "guid": "https://himalayas.app/companies/startupx/jobs/staff-frontend",
            "applicationLink": "https://jobs.ashbyhq.com/startupx/abcdef12-3456-7890",
            "description": "<p>Work from anywhere in the Americas.</p>",
        },
        {
            "title": "Finance Controller",
            "companyName": "BankY",
            "parentCategories": ["Finance"],
            "categories": ["Accounting"],
            "locationRestrictions": ["United States"],
            "guid": "https://himalayas.app/companies/banky/jobs/controller",
            "description": "<p>Accounting role</p>",
        },
    ]
    jobs = scraper._parse_items(items)
    assert len(jobs) == 1
    assert jobs[0]["location"] == "Worldwide"
    assert jobs[0]["posted_at"].startswith("2026-")
    assert detect_platform(jobs[0]["apply_url"], "himalayas") == "ashby"


def test_weworkremotely_rss_parser():
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Acme Corp: Senior Python Engineer</title>
        <region>Anywhere in the World</region>
        <category>Back-End Programming</category>
        <pubDate>Thu, 19 Nov 2026 15:00:00 +0000</pubDate>
        <guid>https://weworkremotely.com/remote-jobs/acme-python</guid>
        <link>https://weworkremotely.com/remote-jobs/acme-python</link>
        <description>&lt;p&gt;Contractor role. Apply via https://jobs.lever.co/acme/12345678-1234-1234-1234-1234567890ab&lt;/p&gt;</description>
      </item>
    </channel></rss>"""
    jobs = WeWorkRemotelyScraper()._parse_rss(rss)
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Acme Corp"
    assert jobs[0]["title"] == "Senior Python Engineer"
    assert jobs[0]["location"] == "Anywhere in the World"
    assert detect_platform(jobs[0]["apply_url"], "weworkremotely") == "lever"


def test_jobicy_and_workingnomads_parsers():
    jc_jobs = JobicyScraper()._parse_items([
        {
            "id": 999,
            "url": "https://jobicy.com/jobs/999-fullstack",
            "jobTitle": "Fullstack Engineer",
            "companyName": "GlobalCo",
            "jobIndustry": ["Software Development"],
            "jobType": ["Full-Time"],
            "jobGeo": "LATAM",
            "jobLevel": "Mid-level",
            "jobDescription": "<p>React + Node.js B2B contractor</p>",
            "pubDate": "2026-11-20T08:00:00+00:00",
            "salaryMin": 70000,
            "salaryMax": 90000,
            "salaryCurrency": "USD",
            "salaryPeriod": "yearly",
        }
    ])
    assert len(jc_jobs) == 1
    assert jc_jobs[0]["id"] == "jc:999"
    assert jc_jobs[0]["salary"] == "USD 70000-90000 yearly"

    wn_jobs = WorkingNomadsScraper()._parse_items([
        {
            "url": "https://www.workingnomads.com/job/go/2034803/",
            "title": "Senior React Developer",
            "company_name": "NomadCo",
            "category_name": "Development",
            "tags": "react,typescript",
            "location": "Anywhere",
            "pub_date": "2026-11-19T07:13:33-05:00",
            "description": "<p>Async worldwide team</p>",
        },
        {
            "url": "https://www.workingnomads.com/job/go/2034804/",
            "title": "Sales Executive",
            "company_name": "SalesCo",
            "category_name": "Sales",
            "location": "USA only",
            "description": "<p>B2B sales</p>",
        },
    ])
    assert len(wn_jobs) == 1
    assert wn_jobs[0]["title"] == "Senior React Developer"
