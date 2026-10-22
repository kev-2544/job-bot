"""Filtering, dedupe, storage and LLM-check behaviour, using realistic postings."""

import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from jobbot.config import DEFAULTS, ConfigError, build_config, deep_merge, load_config
from jobbot.filters import JobFilter, filter_jobs
from jobbot.llm_check import LLMChecker, run_llm_checks
from jobbot.models import Job, make_job_id
from jobbot.storage import LLMVerdict, Storage, dedupe_batch

from conftest import ROOT


def job(title="Senior Python Developer", company="Acme", location="", description="", url=None, source="test"):
    url = url or f"https://example.com/jobs/{abs(hash((title, company, location, description)))}"
    return Job(title=title, company=company, url=url, location_text=location, description=description, source=source)


def evaluate(filter_cfg, j):
    return JobFilter(filter_cfg).evaluate(j)


# --- Freshness ----------------------------------------------------------------

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "age_days, kept",
    [(0, True), (6, True), (7, True), (8, False), (20, False)],
)
def test_jobs_older_than_max_age_are_dropped(filter_cfg, age_days, kept):
    j = job(location="Worldwide")
    j.posted_date = NOW - timedelta(days=age_days)
    result = JobFilter(filter_cfg, now=NOW).evaluate(j)
    assert result.keep is kept, result.reason
    if not kept:
        assert result.reason == f"too old (posted {age_days} days ago)"


def test_job_without_date_is_kept(filter_cfg):
    assert JobFilter(filter_cfg, now=NOW).evaluate(job(location="Worldwide")).keep


def test_naive_dates_are_treated_as_utc(filter_cfg):
    j = job(location="Worldwide")
    j.posted_date = datetime(2026, 10, 1)  # no tzinfo
    assert not JobFilter(filter_cfg, now=NOW).evaluate(j).keep


def test_max_age_zero_disables_the_check(filter_cfg):
    j = job(location="Worldwide")
    j.posted_date = NOW - timedelta(days=365)
    assert JobFilter(replace(filter_cfg, max_age_days=0), now=NOW).evaluate(j).keep


# --- Eligible -----------------------------------------------------------------

@pytest.mark.parametrize(
    "location, description",
    [
        ("Worldwide", "Build APIs in Django."),
        ("Anywhere", "Build APIs in Django."),
        ("Africa, Europe", "Build APIs in Django."),
        ("EMEA", "We hire across EMEA time zones."),
        ("Remote", "We welcome applicants based in Kenya or anywhere in East Africa."),
        ("", "This is a fully remote role; you can work from anywhere."),
        ("Nairobi, Kenya", "Hybrid optional."),
    ],
)
def test_eligible_jobs_are_kept_without_flag(filter_cfg, location, description):
    result = evaluate(filter_cfg, job(location=location, description=description))
    assert result.keep and not result.needs_checking, result.reason


# --- Excluded -----------------------------------------------------------------

@pytest.mark.parametrize(
    "location, description",
    [
        ("USA Only", "Great Python role."),
        ("Worldwide", "We are a worldwide team, but this position is US only."),
        ("Remote", "Candidates must be located in the United States."),
        ("Remote", "Applicants must reside in the US and be available 9-5 EST."),
        ("Anywhere in the US", "Great Python role."),
        ("Remote", "Open to UK residents. There is no visa sponsorship for the UK."),
        ("Remote", "We offer no visa sponsorship; candidates must already live in Canada."),
        ("Remote", "You must be authorized to work in the United States."),
        ("Global", "Work authorization required."),
        ("Remote", "This role is EU only due to payroll."),
        ("Europe only", "Great Python role."),
        ("North America only", ""),
    ],
)
def test_exclusion_rules_reject(filter_cfg, location, description):
    result = evaluate(filter_cfg, job(location=location, description=description))
    assert not result.keep, result.reason
    assert "excluded" in result.reason


@pytest.mark.parametrize("location", ["United States", "USA", "Europe", "Canada", "LATAM", "Remote - UK", "Americas"])
def test_location_field_restricted_to_other_region_rejects(filter_cfg, location):
    result = evaluate(filter_cfg, job(location=location, description="Great Python role."))
    assert not result.keep
    assert "restricted" in result.reason


@pytest.mark.parametrize("location", ["South Africa", "South Africa only", "Nigeria", "North Africa"])
def test_other_african_countries_are_not_kenya(filter_cfg, location):
    result = evaluate(filter_cfg, job(location=location, description="Python role."))
    assert not result.keep, result.reason


def test_south_africa_mention_in_description_does_not_count_as_africa(filter_cfg):
    j = job(location="Remote", description="Our customers are in South Africa. Python role.")
    result = evaluate(filter_cfg, j)
    assert result.keep and result.needs_checking  # ambiguous, not confirmed


def test_kenya_alongside_south_africa_is_eligible(filter_cfg):
    result = evaluate(filter_cfg, job(location="Kenya, South Africa", description="Python role."))
    assert result.keep and not result.needs_checking


def test_allow_keyword_in_location_beats_restricted_region(filter_cfg):
    result = evaluate(filter_cfg, job(location="USA, Europe, Africa", description="Python role."))
    assert result.keep and not result.needs_checking


def test_everyday_words_do_not_trigger_exclusions(filter_cfg):
    # "us" inside ordinary sentences and an untied "no visa sponsorship" must not exclude.
    j = job(location="Worldwide", description="Join us! Contact us to learn more. No visa sponsorship needed - we hire contractors.")
    result = evaluate(filter_cfg, j)
    assert result.keep, result.reason


# --- Role match ---------------------------------------------------------------

@pytest.mark.parametrize(
    "title",
    ["Backend Engineer", "Full-Stack Developer", "Front End Engineer", "DevOps Lead", "Mobile Dev (iOS)", "SRE"],
)
def test_role_titles_match(filter_cfg, title):
    assert evaluate(filter_cfg, job(title=title, location="Worldwide")).keep


def test_role_matched_through_description(filter_cfg):
    j = job(
        title="Member of Technical Staff",
        location="Worldwide",
        description="You will write Python and TypeScript services as a backend developer.",
    )
    assert evaluate(filter_cfg, j).keep


def test_single_incidental_description_keyword_is_not_a_role_match(filter_cfg):
    # Real Remotive listing: an office-assistant posting that mentions "software" once.
    j = job(title="Remote Office Assistant", location="Worldwide", description="Use our project software daily.")
    assert not evaluate(filter_cfg, j).keep
    # Synonyms count once: "full stack" + "full-stack" + "fullstack" is one keyword.
    j = job(title="Coordinator", location="Worldwide", description="full stack, full-stack, fullstack")
    assert not evaluate(filter_cfg, j).keep


def test_non_software_roles_rejected(filter_cfg):
    j = job(title="Customer Success Manager", location="Worldwide", description="Help our customers succeed.")
    result = evaluate(filter_cfg, j)
    assert not result.keep and result.reason == "no role keyword"


def test_title_exclude_keywords(filter_cfg):
    result = evaluate(filter_cfg, job(title="Sales Engineer", location="Worldwide"))
    assert not result.keep and "title excluded" in result.reason


def test_description_match_can_be_disabled(filter_cfg):
    cfg = replace(filter_cfg, match_description=False)
    j = job(
        title="Member of Technical Staff",
        location="Worldwide",
        description="You will write Python and TypeScript services as a backend developer.",
    )
    assert not evaluate(cfg, j).keep


# --- Ambiguous ----------------------------------------------------------------

@pytest.mark.parametrize("location", ["", "Remote", "Flexible"])
def test_ambiguous_location_kept_with_flag(filter_cfg, location):
    result = evaluate(filter_cfg, job(location=location, description="Write Python. Ship features."))
    assert result.keep and result.needs_checking


def test_generic_description_mention_flags_job(filter_cfg):
    j = job(location="Remote", description="We serve global customers with Python.")
    result = evaluate(filter_cfg, j)
    assert result.keep and result.needs_checking
    assert "generic mention" in result.reason


def test_ambiguous_dropped_when_disabled(filter_cfg):
    cfg = replace(filter_cfg, keep_ambiguous=False)
    assert not evaluate(cfg, job(location="Remote", description="Write Python.")).keep


def test_apply_sets_needs_checking_and_reason(filter_cfg):
    jobs = [
        job(title="Python Developer", location="Worldwide"),
        job(title="Go Engineer", location="Remote", description="Ship things."),
        job(title="Ruby Developer", location="USA"),
    ]
    kept = JobFilter(filter_cfg).apply(jobs)
    assert [j.title for j in kept] == ["Python Developer", "Go Engineer"]
    assert kept[0].needs_checking is False and kept[0].check_reason == ""
    assert kept[1].needs_checking is True and kept[1].check_reason == "no location information"


def test_html_descriptions_are_handled(filter_cfg):
    j = job(location="Remote", description="<p>We hire <strong>anywhere&nbsp;in&nbsp;Africa</strong>.</p>")
    result = evaluate(filter_cfg, j)
    assert result.keep and not result.needs_checking


# --- Job identity and cross-source duplicates ---------------------------------

def test_job_id_is_stable_and_ignores_tracking_params():
    a = make_job_id("https://Remotive.com/remote-jobs/software-dev/python-dev-123/?utm_source=api")
    b = make_job_id("https://remotive.com/remote-jobs/software-dev/python-dev-123")
    assert a == b and len(a) == 20
    assert make_job_id("https://remotive.com/remote-jobs/x-1") != make_job_id("https://remotive.com/remote-jobs/x-2")


def test_cross_source_duplicates_share_dedupe_key():
    a = job(title="Senior Backend Engineer (Remote)", company="Acme Inc.", url="https://remotive.com/1", source="remotive")
    b = job(title="Sr. Backend Engineer", company="ACME", url="https://remoteok.com/2", source="remoteok")
    c = job(title="Senior Frontend Engineer", company="Acme", url="https://remoteok.com/3", source="remoteok")
    assert a.dedupe_key == b.dedupe_key
    assert a.dedupe_key != c.dedupe_key


def test_dedupe_batch_removes_cross_source_duplicates():
    a = job(title="Senior Backend Engineer (Remote)", company="Acme Inc.", url="https://remotive.com/1", source="remotive")
    b = job(title="Sr. Backend Engineer", company="ACME", url="https://remoteok.com/2", source="remoteok")
    same_url = job(title="Senior Backend Engineer", company="Acme", url="https://remotive.com/1?utm_source=x")
    other = job(title="Data Engineer", company="Acme", url="https://remoteok.com/9")
    result = dedupe_batch([a, b, same_url, other])
    assert [j.url for j in result] == ["https://remotive.com/1", "https://remoteok.com/9"]


def test_dedupe_batch_prefers_confirmed_listing():
    flagged = job(title="Backend Engineer", company="Acme", url="https://a.com/1", source="wwr")
    flagged.needs_checking = True
    confirmed = job(title="Backend Engineer", company="Acme", url="https://b.com/1", source="remotive")
    result = dedupe_batch([flagged, confirmed])
    assert len(result) == 1 and result[0].source == "remotive"


def test_storage_filters_seen_jobs_across_runs(tmp_path):
    db = tmp_path / "jobs.db"
    first = job(title="Backend Engineer", company="Acme Inc", url="https://remotive.com/1", source="remotive")
    with Storage(db) as store:
        assert store.filter_new([first]) == [first]
        assert store.mark_seen([first]) == 1
        assert store.mark_seen([first]) == 0  # idempotent

    # Second run: same URL, and the same job re-posted on another board.
    again = job(title="Backend Engineer", company="Acme Inc", url="https://remotive.com/1/", source="remotive")
    elsewhere = job(title="Backend Engineer (Remote)", company="Acme", url="https://remoteok.com/77", source="remoteok")
    brand_new = job(title="Mobile Developer", company="Acme", url="https://remoteok.com/78", source="remoteok")
    with Storage(db) as store:
        assert store.filter_new([again, elsewhere, brand_new]) == [brand_new]
        assert store.seen_count() == 1


# --- Optional LLM check -------------------------------------------------------

class FakeChecker:
    model = "fake-model"

    def __init__(self, verdicts):
        self.verdicts = verdicts  # title -> LLMVerdict or Exception
        self.calls = []

    def check(self, j):
        self.calls.append(j.title)
        outcome = self.verdicts[j.title]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def flagged(title):
    j = job(title=title, location="Remote")
    j.needs_checking = True
    j.check_reason = "no location information"
    return j


def test_llm_verdicts_applied_and_cached(filter_cfg, tmp_path):
    llm = replace(filter_cfg.llm, enabled=True)
    jobs = [flagged("A Developer"), flagged("B Developer"), flagged("C Developer")]
    checker = FakeChecker({
        "A Developer": LLMVerdict(True, "Open worldwide"),
        "B Developer": LLMVerdict(False, "US only"),
        "C Developer": LLMVerdict(None, "No location given"),
    })
    with Storage(tmp_path / "jobs.db") as store:
        result = run_llm_checks(jobs, llm, checker, store)
        assert [j.title for j in result] == ["A Developer", "C Developer"]
        assert result[0].needs_checking is False
        assert result[1].needs_checking is True and result[1].check_reason == "LLM: No location given"

        # Second run uses the cache: no new API calls.
        again = run_llm_checks([flagged("A Developer"), flagged("B Developer")], llm, checker, store)
        assert [j.title for j in again] == ["A Developer"]
        assert len(checker.calls) == 3


def test_llm_scope_ambiguous_skips_confirmed_jobs(filter_cfg):
    llm = replace(filter_cfg.llm, enabled=True, scope="ambiguous")
    confirmed = job(title="Confirmed Developer", location="Worldwide")
    checker = FakeChecker({})
    assert run_llm_checks([confirmed], llm, checker) == [confirmed]
    assert checker.calls == []


def test_llm_failure_keeps_job_flagged(filter_cfg):
    llm = replace(filter_cfg.llm, enabled=True, fail_open=True)
    checker = FakeChecker({"A Developer": RuntimeError("API down")})
    result = run_llm_checks([flagged("A Developer")], llm, checker)
    assert len(result) == 1 and result[0].needs_checking

    llm_closed = replace(llm, fail_open=False)
    assert run_llm_checks([flagged("A Developer")], llm_closed, checker) == []


def test_llm_call_limit(filter_cfg):
    llm = replace(filter_cfg.llm, enabled=True, max_checks_per_run=1)
    checker = FakeChecker({"A Developer": LLMVerdict(False, "no"), "B Developer": LLMVerdict(False, "no")})
    result = run_llm_checks([flagged("A Developer"), flagged("B Developer")], llm, checker)
    assert [j.title for j in result] == ["B Developer"]  # second job left as is, still flagged
    assert checker.calls == ["A Developer"]


def test_filter_jobs_runs_llm_when_enabled(filter_cfg):
    cfg = replace(filter_cfg, llm=replace(filter_cfg.llm, enabled=True))
    jobs = [job(title="Go Developer", location="Remote", description="Ship code.")]
    checker = FakeChecker({"Go Developer": LLMVerdict(True, "Hires contractors in 40+ countries incl. Kenya")})
    result = filter_jobs(jobs, cfg, checker=checker)
    assert len(result) == 1 and result[0].needs_checking is False


def test_filter_jobs_without_api_key_skips_llm(filter_cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = replace(filter_cfg, llm=replace(filter_cfg.llm, enabled=True))
    result = filter_jobs([job(title="Go Developer", location="Remote", description="Ship code.")], cfg)
    assert len(result) == 1 and result[0].needs_checking is True


def test_llm_checker_parses_structured_response():
    calls = {}

    def create(**kwargs):
        calls.update(kwargs)
        text = json.dumps({"eligible_for_kenya": "false", "reason": "Requires US work authorization"})
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    checker = LLMChecker("claude-sonnet-4-6", api_key="x", max_description_chars=20, client=client)
    verdict = checker.check(job(description="x" * 100))
    assert verdict.eligible is False and verdict.reason == "Requires US work authorization"
    assert calls["model"] == "claude-sonnet-4-6"
    assert calls["output_config"]["format"]["type"] == "json_schema"
    assert "x" * 21 not in calls["messages"][0]["content"]


def test_llm_checker_handles_refusal():
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(stop_reason="refusal", content=[])
    ))
    verdict = LLMChecker("m", api_key="x", client=client).check(job())
    assert verdict.eligible is None


# --- Config -------------------------------------------------------------------

def test_config_yaml_matches_builtin_defaults():
    """config.yaml and DEFAULTS should not drift apart."""
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert data == deep_merge(DEFAULTS, {"http": {"user_agent": data["http"]["user_agent"]}})


def test_missing_config_file_uses_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg.filters.llm.enabled is False
    assert cfg.storage.db_path == "data/jobs.db"
    assert cfg.source_enabled("remotive")


def test_deep_merge_replaces_lists_and_merges_dicts():
    merged = deep_merge(DEFAULTS, {"filters": {"role_keywords": ["rust"], "llm": {"enabled": True}}})
    cfg = build_config(merged)
    assert cfg.filters.role_keywords == ["rust"]
    assert cfg.filters.llm.enabled is True and cfg.filters.llm.model == "claude-sonnet-4-6"


def test_invalid_regex_and_scope_raise():
    bad = copy.deepcopy(DEFAULTS)
    bad["filters"]["exclude_patterns"] = ["(unclosed"]
    with pytest.raises(ConfigError):
        build_config(bad)
    bad = deep_merge(DEFAULTS, {"filters": {"llm": {"scope": "sometimes"}}})
    with pytest.raises(ConfigError):
        build_config(bad)
