"""Telegram and email notifiers with mocked network, plus channel isolation."""

import smtplib
from datetime import datetime, timezone
from email import message_from_bytes

import pytest
import requests

from jobbot.config import HttpConfig
from jobbot.models import Job
from jobbot.notifiers import Notifier, NotifierError, notify_all
from jobbot.notifiers import emailer as emailer_mod
from jobbot.notifiers.emailer import EmailNotifier, render_html, render_text
from jobbot.notifiers.telegram import MAX_MESSAGE_LEN, TelegramNotifier, build_digest_messages, tg_len

HTTP = HttpConfig(timeout_seconds=5, max_retries=0, backoff_factor=0, user_agent="test")


def make_job(i=1, **kw):
    defaults = dict(
        title=f"Backend Engineer {i}",
        company="Acme <Labs> & Co",
        url=f"https://remotive.com/jobs/{i}?a=1&b=2",
        location_text="Worldwide",
        source="remotive",
        posted_date=datetime(2026, 10, 20, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return Job(**defaults)


@pytest.fixture
def telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


class FakeResponse:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._data = {"ok": status == 200} if data is None else data
        self.headers = headers or {}
        self.reason = "reason"

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        outcome = self.responses.pop(0) if self.responses else FakeResponse()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def telegram(session, threshold=10):
    sleeps = []
    notifier = TelegramNotifier(HTTP, digest_threshold=threshold, session=session, sleep=sleeps.append, min_interval=0)
    return notifier, sleeps


# --- Telegram -------------------------------------------------------------------

def test_telegram_one_message_per_job(telegram_env):
    session = FakeSession([])
    notifier, _ = telegram(session)
    jobs = [make_job(1), make_job(2, needs_checking=True, check_reason="no location information")]
    notifier.send(jobs)

    assert len(session.calls) == 2
    url, payload = session.calls[0]
    assert url == "https://api.telegram.org/bot123:SECRET/sendMessage"
    assert payload["chat_id"] == "42" and payload["parse_mode"] == "HTML"
    text = payload["text"]
    assert 'href="https://remotive.com/jobs/1?a=1&amp;b=2"' in text
    assert "Acme &lt;Labs&gt; &amp; Co" in text  # HTML-escaped
    assert "via Remotive" in text and "Needs checking" not in text
    assert "Needs checking: no location information" in session.calls[1][1]["text"]


def test_telegram_digest_above_threshold(telegram_env):
    session = FakeSession([])
    notifier, _ = telegram(session, threshold=10)
    notifier.send([make_job(i) for i in range(11)])
    assert len(session.calls) == 1
    text = session.calls[0][1]["text"]
    assert text.startswith("<b>11 new Kenya-eligible remote software jobs</b>")
    assert "11. <a href=" in text


def test_digest_splits_at_message_limit():
    jobs = [make_job(i, title="Senior Platform Engineer " + "x" * 150) for i in range(60)]
    messages = build_digest_messages(jobs)
    assert len(messages) > 1
    assert all(tg_len(m) <= MAX_MESSAGE_LEN for m in messages)
    assert f"part 1/{len(messages)}" in messages[0]
    joined = "\n".join(messages)
    assert all(f"{i}. <a href" in joined for i in range(1, 61))


def test_telegram_waits_on_rate_limit(telegram_env):
    session = FakeSession([FakeResponse(429, {"ok": False, "parameters": {"retry_after": 7}}), FakeResponse()])
    notifier, sleeps = telegram(session)
    notifier.send([make_job()])
    assert len(session.calls) == 2
    assert 7.5 in sleeps


def test_telegram_retries_connection_errors_then_succeeds(telegram_env):
    session = FakeSession([requests.ConnectionError("boom"), FakeResponse()])
    notifier, _ = telegram(session)
    notifier.send([make_job()])
    assert len(session.calls) == 2


def test_telegram_bad_request_fails_fast_without_leaking_token(telegram_env):
    session = FakeSession([FakeResponse(401, {"ok": False, "description": "Unauthorized"})])
    notifier, _ = telegram(session)
    with pytest.raises(NotifierError) as err:
        notifier.send([make_job()])
    assert len(session.calls) == 1
    assert "SECRET" not in str(err.value)


def test_telegram_gives_up_after_max_attempts(telegram_env):
    session = FakeSession([FakeResponse(502, {"ok": False})] * 10)
    notifier, _ = telegram(session)
    with pytest.raises(NotifierError, match="after 4 attempts"):
        notifier.send([make_job()])
    assert len(session.calls) == 4


def test_telegram_missing_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    notifier, _ = telegram(FakeSession([]))
    assert notifier.missing_env() == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]


# --- Email ----------------------------------------------------------------------

@pytest.fixture
def email_env(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("EMAIL_TO", "me@gmail.com, other@example.com")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_PORT", raising=False)
    monkeypatch.delenv("EMAIL_FROM", raising=False)


class FakeSMTP:
    instances = []
    fail_with = []  # exceptions raised by successive send_message calls

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.sent = []
        self.actions = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.actions.append("ehlo")

    def starttls(self, context=None):
        self.actions.append("starttls")

    def login(self, user, password):
        self.actions.append(("login", user))

    def send_message(self, msg):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with.pop(0)
        self.sent.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_with = []
    monkeypatch.setattr(emailer_mod.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(emailer_mod.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_email_digest_over_ssl_defaults_to_gmail(email_env, fake_smtp):
    jobs = [make_job(1), make_job(2, needs_checking=True, check_reason="unclear location 'Flexible'")]
    EmailNotifier(sleep=lambda s: None).send(jobs)

    smtp = fake_smtp.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 465)
    assert smtp.context is not None and "starttls" not in smtp.actions
    msg = message_from_bytes(smtp.sent[0].as_bytes())
    assert msg["Subject"].startswith("2 new Kenya-eligible remote software jobs")
    assert msg["To"] == "me@gmail.com, other@example.com"
    assert msg["From"] == "me@gmail.com"
    parts = {p.get_content_type(): p.get_payload(decode=True).decode() for p in msg.walk() if not p.is_multipart()}
    assert set(parts) == {"text/plain", "text/html"}
    assert "NEEDS CHECKING: unclear location 'Flexible'" in parts["text/plain"]
    assert "needs checking</span>" in parts["text/html"]
    assert 'href="https://remotive.com/jobs/1?a=1&amp;b=2"' in parts["text/html"]


def test_email_uses_starttls_on_other_ports(email_env, fake_smtp, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    EmailNotifier(sleep=lambda s: None).send([make_job()])
    assert fake_smtp.instances[0].actions[:3] == ["ehlo", "starttls", "ehlo"]


def test_email_retries_transient_errors(email_env, fake_smtp):
    fake_smtp.fail_with = [smtplib.SMTPServerDisconnected("dropped")]
    EmailNotifier(sleep=lambda s: None).send([make_job()])
    assert len(fake_smtp.instances) == 2 and fake_smtp.instances[1].sent


def test_email_auth_failure_is_not_retried(email_env, fake_smtp):
    fake_smtp.fail_with = [smtplib.SMTPAuthenticationError(535, b"bad credentials")]
    with pytest.raises(NotifierError, match="SMTP login failed"):
        EmailNotifier(sleep=lambda s: None).send([make_job()])
    assert len(fake_smtp.instances) == 1


def test_email_render_escapes_html():
    html = render_html([make_job(title="<script>x</script> Dev")])
    assert "<script>" not in html and "&lt;script&gt;" in html
    text = render_text([make_job(title="Go Dev")])
    assert "1. Go Dev" in text and "Source:   Remotive (posted 20 Oct 2026)" in text


# --- Channel isolation ----------------------------------------------------------

class StubNotifier(Notifier):
    def __init__(self, name, fail=False, required=()):
        self.name = name
        self.required_env = required
        self.fail = fail
        self.sent = None
        self.empty_notice = False

    def send(self, jobs):
        if self.fail:
            raise RuntimeError(f"{self.name} down")
        self.sent = jobs

    def send_empty_notice(self):
        self.empty_notice = True


def test_one_channel_failing_does_not_stop_the_other():
    broken, working = StubNotifier("telegram", fail=True), StubNotifier("email")
    results = notify_all([broken, working], [make_job()])
    assert results == {"telegram": False, "email": True}
    assert working.sent is not None


def test_missing_env_counts_as_failure(monkeypatch):
    monkeypatch.delenv("NOPE_VAR", raising=False)
    results = notify_all([StubNotifier("telegram", required=("NOPE_VAR",))], [make_job()])
    assert results == {"telegram": False}


def test_no_jobs_sends_nothing_unless_empty_notice_enabled():
    stub = StubNotifier("email")
    assert notify_all([stub], [], send_empty_notice=False) == {}
    assert stub.empty_notice is False
    assert notify_all([stub], [], send_empty_notice=True) == {"email": True}
    assert stub.empty_notice is True
