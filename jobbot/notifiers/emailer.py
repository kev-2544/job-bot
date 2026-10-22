"""Email digest over SMTP (SSL on port 465, STARTTLS otherwise). Defaults to Gmail.

Named emailer.py (not email.py) so it can never shadow the standard library
`email` package.
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Callable

from ..config import get_env
from ..models import Job
from .base import EMPTY_NOTICE, Notifier, NotifierError, format_date, location_label, source_label, subject_line

log = logging.getLogger(__name__)

DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 465


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _summary(jobs: list[Job]) -> str:
    flagged = sum(j.needs_checking for j in jobs)
    text = f"{len(jobs)} new job{'s' if len(jobs) != 1 else ''}"
    if flagged:
        text += f", {flagged} marked 'needs checking' (location policy unclear - read the posting before applying)"
    return text + "."


def render_text(jobs: list[Job]) -> str:
    lines = [subject_line(len(jobs)), "", _summary(jobs), ""]
    for i, job in enumerate(jobs, start=1):
        lines.append(f"{i}. {job.title}")
        lines.append(f"   Company:  {job.company}")
        lines.append(f"   Location: {location_label(job)}")
        source = source_label(job.source)
        if job.posted_date:
            source += f" (posted {format_date(job.posted_date)})"
        lines.append(f"   Source:   {source}")
        if job.needs_checking:
            lines.append(f"   NEEDS CHECKING: {job.check_reason or 'location unclear'}")
        lines.append(f"   {job.url}")
        lines.append("")
    return "\n".join(lines)


def render_html(jobs: list[Job]) -> str:
    cards = []
    for job in jobs:
        badge = ""
        if job.needs_checking:
            badge = (
                '<span style="display:inline-block;background:#fff3cd;color:#7a5a00;border:1px solid #f0d58a;'
                'border-radius:4px;padding:1px 6px;font-size:12px;margin-left:6px;">needs checking</span>'
            )
        meta = f"via {_esc(source_label(job.source))}"
        if job.posted_date:
            meta += f" · posted {format_date(job.posted_date)}"
        reason = ""
        if job.needs_checking and job.check_reason:
            reason = f'<div style="color:#7a5a00;font-size:13px;margin-top:4px;">{_esc(job.check_reason)}</div>'
        cards.append(
            '<div style="border:1px solid #e3e3e3;border-radius:6px;padding:12px 14px;margin:0 0 10px 0;">'
            f'<div style="font-size:16px;font-weight:600;"><a href="{_esc(job.url)}" style="color:#1a56db;'
            f'text-decoration:none;">{_esc(job.title)}</a>{badge}</div>'
            f'<div style="color:#333;margin-top:4px;">{_esc(job.company)}</div>'
            f'<div style="color:#555;font-size:14px;margin-top:2px;">📍 {_esc(location_label(job))}</div>'
            f'<div style="color:#888;font-size:12px;margin-top:4px;">{meta}</div>'
            f"{reason}</div>"
        )
    return (
        '<!doctype html><html><body style="margin:0;padding:16px;background:#ffffff;'
        'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#222;">'
        '<div style="max-width:640px;margin:0 auto;">'
        f'<h2 style="font-size:20px;margin:0 0 6px 0;">{_esc(subject_line(len(jobs)))}</h2>'
        f'<p style="color:#555;margin:0 0 16px 0;">{_esc(_summary(jobs))}</p>'
        + "".join(cards)
        + '<p style="color:#999;font-size:12px;margin-top:16px;">Sent by your job-alert bot. '
        "Listings link back to the original job boards.</p></div></body></html>"
    )


def build_message(subject: str, text: str, html_body: str | None, sender: str, recipients: list[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


class EmailNotifier(Notifier):
    name = "email"
    required_env = ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO")

    def __init__(self, timeout: float = 30.0, max_attempts: int = 3, sleep: Callable[[float], None] = time.sleep) -> None:
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.sleep = sleep

    @staticmethod
    def recipients() -> list[str]:
        return [r.strip() for r in (get_env("EMAIL_TO") or "").split(",") if r.strip()]

    def send(self, jobs: list[Job]) -> None:
        today = datetime.now(timezone.utc).strftime("%d %b %Y")
        subject = f"{subject_line(len(jobs))} – {today}"
        self._deliver(subject, render_text(jobs), render_html(jobs))
        log.info("Email: sent digest with %d job(s) to %d recipient(s)", len(jobs), len(self.recipients()))

    def send_empty_notice(self) -> None:
        self._deliver("No new Kenya-eligible remote software jobs", EMPTY_NOTICE, None)

    def _deliver(self, subject: str, text: str, html_body: str | None) -> None:
        host = get_env("SMTP_HOST", DEFAULT_HOST)
        try:
            port = int(get_env("SMTP_PORT", str(DEFAULT_PORT)))
        except ValueError:
            raise NotifierError(f"SMTP_PORT must be a number, got {get_env('SMTP_PORT')!r}") from None
        user = get_env("SMTP_USER")
        password = get_env("SMTP_PASSWORD")
        recipients = self.recipients()
        if not user or not password or not recipients:
            raise NotifierError("SMTP_USER, SMTP_PASSWORD and EMAIL_TO must be set")
        sender = get_env("EMAIL_FROM", user)
        msg = build_message(subject, text, html_body, sender, recipients)
        context = ssl.create_default_context()

        last_error = "unknown error"
        for attempt in range(1, self.max_attempts + 1):
            try:
                if port == 465:
                    with smtplib.SMTP_SSL(host, port, timeout=self.timeout, context=context) as smtp:
                        smtp.login(user, password)
                        smtp.send_message(msg)
                else:
                    with smtplib.SMTP(host, port, timeout=self.timeout) as smtp:
                        smtp.ehlo()
                        smtp.starttls(context=context)
                        smtp.ehlo()
                        smtp.login(user, password)
                        smtp.send_message(msg)
                return
            except smtplib.SMTPAuthenticationError as exc:
                # Retrying will not fix credentials. For Gmail this usually means an
                # App Password is required (normal account passwords are rejected).
                raise NotifierError(f"SMTP login failed for {user} on {host}:{port} ({exc.smtp_code})") from None
            except (smtplib.SMTPException, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Email attempt %d/%d failed: %s", attempt, self.max_attempts, last_error)
                if attempt < self.max_attempts:
                    self.sleep(2**attempt)
        raise NotifierError(f"Email failed after {self.max_attempts} attempts: {last_error}")
