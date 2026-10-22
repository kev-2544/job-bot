"""Source parsing with canned responses, source discovery, and the run pipeline."""

import io
from dataclasses import replace
from pathlib import Path

import pytest

from jobbot import app
from jobbot.http import HttpError, redact
from jobbot.models import Job
from jobbot.notifiers import Notifier
from jobbot.sources import build_sources, discover_sources, fetch_all
from jobbot.sources.adzuna import AdzunaSource
from jobbot.sources.base import Source, parse_date
from jobbot.sources.jooble import JoobleSource
from jobbot.sources.remoteok import RemoteOKSource
from jobbot.sources.remotive import RemotiveSource
from jobbot.sources.weworkremotely import WeWorkRemotelySource


class FakeHttp:
    """Stands in for HttpClient: returns canned payloads keyed by URL prefix."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _lookup(self, url):
        self.calls.append(url)
        for prefix, payload in self.routes.items():
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise HttpError(f"no route for {url}")

    def get_json(self, url, params=None, **kw):
        self.calls.append(params)
        return self._lookup(url)

    def post_json(self, url, payload, **kw):
        self.calls.append(payload)
        return self._lookup(url)

    def get_bytes(self, url, **kw):
        return self._lookup(url)


REMOTIVE = {
    "0-legal-notice": "...",
    "jobs": [
        {
            "url": "https://remotive.com/remote-jobs/software-development/python-dev-1",
            "title": "Python Developer",
            "company_name": "Acme",
            "category": "Software Development",
            "candidate_required_location": "Worldwide",
            "publication_date": "2026-10-17T16:04:29",
            "description": "<p>Build <b>APIs</b>.</p>",
            "tags": ["python"],
        },
        {"url": "https://remotive.com/x", "title": "Copywriter", "category": "Writing", "company_name": "B"},
    ],
}

REMOTEOK = [
    {"legal": "API Terms of Service ..."},
    {
        "position": "Senior Go Engineer",
        "company": "Gopher Inc",
        "url": "https://remoteOK.com/remote-jobs/remote-go-1",
        "location": "Worldwide",
        "description": "<p>Go &amp; Kubernetes</p>",
        "tags": ["golang", "devops"],
        "date": "2026-10-21T00:00:06+00:00",
    },
    {"position": "", "url": "https://remoteok.com/bad"},
]

WWR_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>WWR</title>
<item>
  <title>DuckDuckGo: Senior Backend Engineer</title>
  <region>Anywhere in the World</region>
  <link>https://weworkremotely.com/remote-jobs/duckduckgo-senior-backend-engineer</link>
  <description>&lt;p&gt;Work on search.&lt;/p&gt;</description>
  <pubDate>Wed, 21 Oct 2026 13:46:32 +0000</pubDate>
</item>
</channel></rss>"""


def test_remotive_parses_and_filters_categories():
    jobs = RemotiveSource({"url": "https://remotive.test/api", "categories": ["Software Development"]},
                          FakeHttp({"https://remotive.test": REMOTIVE})).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert (j.title, j.company, j.location_text, j.source) == ("Python Developer", "Acme", "Worldwide", "remotive")
    assert j.description == "Build APIs ."
    assert j.posted_date.year == 2026 and j.posted_date.tzinfo is not None


def test_remoteok_skips_legal_notice_and_bad_items():
    jobs = RemoteOKSource({"url": "https://remoteok.test/api"}, FakeHttp({"https://remoteok.test": REMOTEOK})).fetch()
    assert len(jobs) == 1
    assert jobs[0].title == "Senior Go Engineer"
    assert "Tags: golang, devops" in jobs[0].description
    assert "Go & Kubernetes" in jobs[0].description


def test_wwr_splits_company_and_title_and_survives_one_bad_feed():
    http = FakeHttp({"https://good": WWR_RSS, "https://bad": HttpError("HTTP 500")})
    jobs = WeWorkRemotelySource({"feeds": ["https://bad/feed.rss", "https://good/feed.rss"]}, http).fetch()
    assert len(jobs) == 1
    assert (jobs[0].company, jobs[0].title) == ("DuckDuckGo", "Senior Backend Engineer")
    assert jobs[0].location_text == "Anywhere in the World"
    assert jobs[0].posted_date is not None


def test_wwr_all_feeds_failing_raises():
    http = FakeHttp({"https://bad": HttpError("HTTP 500")})
    with pytest.raises(RuntimeError):
        WeWorkRemotelySource({"feeds": ["https://bad/1", "https://bad/2"]}, http).fetch()


def test_jooble_posts_query(monkeypatch):
    monkeypatch.setenv("JOOBLE_API_KEY", "jkey")
    payload = {"jobs": [{"title": "<b>Software</b> Developer", "company": "Safari", "link": "https://jooble.org/j/1",
                         "location": "Nairobi", "snippet": "Remote OK", "updated": "2026-10-20T10:00:00.000"}]}
    http = FakeHttp({"https://jooble.org/api/jkey": payload})
    jobs = JoobleSource({"keywords": "software developer", "location": "Kenya"}, http).fetch()
    assert jobs[0].title == "Software Developer" and jobs[0].location_text == "Nairobi"
    assert {"keywords": "software developer", "location": "Kenya", "page": "1"} in http.calls


def test_adzuna_builds_location_from_area(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    payload = {"results": [{"title": "Python Developer", "redirect_url": "https://adzuna.test/1",
                            "company": {"display_name": "Brit Ltd"},
                            "location": {"display_name": "London", "area": ["UK", "London"]},
                            "description": "Remote within the UK", "created": "2026-10-20T10:00:00Z"}]}
    http = FakeHttp({"https://api.adzuna.com/v1/api/jobs/gb/search/1": payload})
    jobs = AdzunaSource({"countries": ["gb"]}, http).fetch()
    assert jobs[0].location_text == "London, UK"
    assert jobs[0].company == "Brit Ltd"


def test_parse_date_formats():
    assert parse_date("2026-10-21T00:00:06+00:00").day == 21
    assert parse_date("Wed, 21 Oct 2026 13:46:32 +0000").hour == 13
    assert parse_date(1792540806).year == 2026
    assert parse_date("not a date") is None and parse_date(None) is None


def test_redact_hides_secrets(monkeypatch):
    monkeypatch.setenv("JOOBLE_API_KEY", "supersecretkey")
    assert redact("404 for https://jooble.org/api/supersecretkey") == "404 for https://jooble.org/api/<JOOBLE_API_KEY>"


# --- Discovery ------------------------------------------------------------------

def test_all_five_sources_discovered():
    assert {"remotive", "remoteok", "weworkremotely", "jooble", "adzuna"} <= set(discover_sources())


def test_sources_missing_keys_are_skipped(config, monkeypatch):
    for var in ("JOOBLE_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
        monkeypatch.delenv(var, raising=False)
    names = [s.name for s in build_sources(config, http=None)]
    assert names == ["remotive", "remoteok", "weworkremotely"]  # config order preserved


def test_only_source_runs_even_if_disabled(config):
    config.sources["remoteok"]["enabled"] = False
    assert [s.name for s in build_sources(config, http=None, only="remoteok")] == ["remoteok"]
    with pytest.raises(KeyError):
        build_sources(config, http=None, only="linkedin")


def test_fetch_all_isolates_failing_source():
    class Boom(Source):
        name = "boom"

        def fetch(self):
            raise HttpError("HTTP 503")

    class Fine(Source):
        name = "fine"

        def fetch(self):
            return [Job(title="Dev", company="X", url="https://x/1")]

    jobs, stats = fetch_all([Boom({}, None), Fine({}, None)])
    assert len(jobs) == 1 and jobs[0].source == "fine"
    assert stats == {"boom": None, "fine": 1}


# --- Pipeline -------------------------------------------------------------------

class StubSource(Source):
    name = "stub"
    jobs: list = []

    def fetch(self):
        return [Job(**vars(j)) for j in StubSource.jobs]  # fresh copies per run


class RecordingNotifier(Notifier):
    def __init__(self, name, ok=True):
        self.name = name
        self.ok = ok
        self.batches = []

    def send(self, jobs):
        self.batches.append(jobs)
        if not self.ok:
            raise RuntimeError("down")

    def send_empty_notice(self):
        pass


@pytest.fixture
def pipeline(config, tmp_path, monkeypatch):
    StubSource.jobs = [
        Job(title="Python Developer", company="Acme", url="https://a/1", location_text="Worldwide"),
        Job(title="Python Developer (Remote)", company="Acme Inc", url="https://b/1", location_text="Worldwide"),
        Job(title="Go Engineer", company="Beta", url="https://a/2", location_text="Remote", description="Ship Go."),
        Job(title="Java Developer", company="Gamma", url="https://a/3", location_text="USA only"),
    ]
    monkeypatch.setattr(app, "build_sources", lambda cfg, http, only=None: [StubSource({}, None)])
    config.storage.db_path = str(tmp_path / "jobs.db")
    return config


def test_run_sends_new_jobs_once_and_saves_them(pipeline):
    channel = RecordingNotifier("telegram")
    first = app.run(pipeline, notifiers=[channel], http=object())
    assert [j.title for j in first.new] == ["Python Developer", "Go Engineer"]  # dup + US-only removed
    assert first.new[1].needs_checking  # flagged jobs sorted last
    assert first.saved == 2

    second = app.run(pipeline, notifiers=[channel], http=object())
    assert second.new == [] and len(channel.batches) == 1


def test_run_does_not_save_when_every_channel_fails(pipeline):
    result = app.run(pipeline, notifiers=[RecordingNotifier("telegram", ok=False), RecordingNotifier("email", ok=False)],
                     http=object())
    assert result.all_channels_failed and result.saved == 0
    retry = app.run(pipeline, notifiers=[RecordingNotifier("email")], http=object())
    assert len(retry.new) == 2 and retry.saved == 2  # delivered on the next run


def test_run_saves_when_one_channel_succeeds(pipeline):
    result = app.run(pipeline, notifiers=[RecordingNotifier("telegram", ok=False), RecordingNotifier("email")],
                     http=object())
    assert result.saved == 2 and not result.all_channels_failed


def test_dry_run_sends_and_saves_nothing(pipeline):
    channel = RecordingNotifier("telegram")
    result = app.run(pipeline, dry_run=True, notifiers=[channel], http=object())
    assert len(result.new) == 2 and channel.batches == []
    assert not Path(pipeline.storage.db_path).exists()


def test_max_jobs_per_run_defers_the_rest(pipeline):
    pipeline.notifications = replace(pipeline.notifications, max_jobs_per_run=1)
    result = app.run(pipeline, notifiers=[RecordingNotifier("email")], http=object())
    assert len(result.new) == 1 and result.deferred == 1
    later = app.run(pipeline, notifiers=[RecordingNotifier("email")], http=object())
    assert [j.title for j in later.new] == ["Go Engineer"]


def test_print_jobs_output():
    out = io.StringIO()
    job = Job(title="Go Dev", company="Beta", url="https://a/2", source="remoteok", needs_checking=True,
              check_reason="no location information")
    app.print_jobs([job], out)
    text = out.getvalue()
    assert "Go Dev - Beta  [NEEDS CHECKING: no location information]" in text
    assert "Remote OK" in text and "1 new job(s), 1 need checking." in text


def test_main_rejects_unknown_source(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    assert app.main(["--source", "linkedin", "--dry-run", "--config", str(Path(__file__).parent.parent / "config.yaml")]) == 2


def test_seed_marks_everything_seen_without_sending(pipeline):
    pipeline.notifications = replace(pipeline.notifications, max_jobs_per_run=1)
    channel = RecordingNotifier("telegram")
    seeded = app.run(pipeline, seed=True, notifiers=[channel], http=object())
    assert seeded.saved == 2 and channel.batches == []  # cap does not apply to seeding
    assert app.run(pipeline, notifiers=[channel], http=object()).new == []


def test_seed_with_dry_run_is_rejected(pipeline):
    with pytest.raises(ValueError):
        app.run(pipeline, seed=True, dry_run=True, http=object())
