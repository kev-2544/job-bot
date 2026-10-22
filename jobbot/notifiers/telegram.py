"""Telegram Bot API notifier (plain HTTPS calls, HTML parse mode).

Up to `digest_threshold` jobs: one message per job. More: a numbered digest
split into as many messages as the 4096-character limit requires.
"""

from __future__ import annotations

import html
import logging
import time
from typing import Callable

import requests

from ..config import HttpConfig, get_env
from ..http import make_session, redact
from ..models import Job
from .base import EMPTY_NOTICE, Notifier, NotifierError, format_date, location_label, source_label, subject_line

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4096
MAX_TITLE_LEN = 200


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_job_message(job: Job) -> str:
    lines = [
        f'<b><a href="{_esc(job.url)}">{_esc(_short(job.title, MAX_TITLE_LEN))}</a></b>',
        f"🏢 {_esc(_short(job.company, 150))}",
        f"📍 {_esc(_short(location_label(job), 200))}",
    ]
    meta = f"🔎 via {_esc(source_label(job.source))}"
    if job.posted_date:
        meta += f" · posted {format_date(job.posted_date)}"
    lines.append(meta)
    if job.needs_checking:
        lines.append(f"⚠️ <i>Needs checking: {_esc(_short(job.check_reason or 'location unclear', 300))}</i>")
    return _short_html("\n".join(lines))


def format_digest_entry(index: int, job: Job) -> str:
    first = f'{index}. <a href="{_esc(job.url)}">{_esc(_short(job.title, MAX_TITLE_LEN))}</a> — {_esc(_short(job.company, 150))}'
    second = f"    📍 {_esc(_short(location_label(job), 120))} · {_esc(source_label(job.source))}"
    if job.needs_checking:
        second += " · ⚠️ check"
    return f"{first}\n{second}"


def tg_len(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units; emoji count as 2)."""
    return len(text.encode("utf-16-le")) // 2


def _short_html(text: str) -> str:
    # Messages are built from bounded parts, so this only guards against surprises.
    while tg_len(text) > MAX_MESSAGE_LEN:
        text = text[: MAX_MESSAGE_LEN - 200] + "…"
    return text


def build_digest_messages(jobs: list[Job], limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Pack numbered entries into messages no longer than `limit`, each with a header."""
    header_room = 120
    chunks: list[list[str]] = [[]]
    size = 0
    for i, job in enumerate(jobs, start=1):
        entry = format_digest_entry(i, job)
        if chunks[-1] and size + tg_len(entry) + 2 > limit - header_room:
            chunks.append([])
            size = 0
        chunks[-1].append(entry)
        size += tg_len(entry) + 2

    flagged = sum(j.needs_checking for j in jobs)
    title = subject_line(len(jobs))
    if flagged:
        title += f" ({flagged} need checking)"
    messages = []
    for part, entries in enumerate(chunks, start=1):
        header = f"<b>{_esc(title)}</b>"
        if len(chunks) > 1:
            header += f" — part {part}/{len(chunks)}"
        messages.append(_short_html(header + "\n\n" + "\n\n".join(entries)))
    return messages


class TelegramNotifier(Notifier):
    name = "telegram"
    required_env = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

    def __init__(
        self,
        http_cfg: HttpConfig,
        digest_threshold: int = 10,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        min_interval: float = 1.1,
        max_attempts: int = 4,
    ) -> None:
        self.http_cfg = http_cfg
        self.digest_threshold = digest_threshold
        # Status codes are handled below (429 needs Telegram's retry_after), so the
        # session itself only retries connection problems.
        self.session = session or make_session(http_cfg, retry_statuses=())
        self.sleep = sleep
        self.min_interval = min_interval  # Telegram allows ~1 message/second per chat
        self.max_attempts = max_attempts
        self._last_sent = 0.0

    def build_messages(self, jobs: list[Job]) -> list[str]:
        if len(jobs) > self.digest_threshold:
            return build_digest_messages(jobs)
        return [format_job_message(job) for job in jobs]

    def send(self, jobs: list[Job]) -> None:
        messages = self.build_messages(jobs)
        for text in messages:
            self._send_message(text)
        log.info("Telegram: sent %d message(s) for %d job(s)", len(messages), len(jobs))

    def send_empty_notice(self) -> None:
        self._send_message(_esc(EMPTY_NOTICE))

    def _pace(self) -> None:
        wait = self._last_sent + self.min_interval - time.monotonic()
        if wait > 0:
            self.sleep(wait)

    def _send_message(self, text: str) -> None:
        token = get_env("TELEGRAM_BOT_TOKEN")
        chat_id = get_env("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise NotifierError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        url = f"{API_BASE}/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}

        last_error = "unknown error"
        for attempt in range(1, self.max_attempts + 1):
            self._pace()
            try:
                response = self.session.post(url, json=payload, timeout=self.http_cfg.timeout_seconds)
            except requests.RequestException as exc:
                last_error = redact(f"{type(exc).__name__}: {exc}")
                self.sleep(self.http_cfg.backoff_factor * 2 ** (attempt - 1))
                continue
            finally:
                self._last_sent = time.monotonic()

            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.status_code == 200 and data.get("ok", True):
                return
            description = data.get("description") or response.reason or ""
            last_error = f"HTTP {response.status_code}: {description}"
            if response.status_code == 429:
                retry_after = (data.get("parameters") or {}).get("retry_after") or response.headers.get("Retry-After") or 5
                wait = min(float(retry_after), 60.0) + 0.5
                log.warning("Telegram rate limit hit; waiting %.1fs (attempt %d)", wait, attempt)
                self.sleep(wait)
                continue
            if response.status_code >= 500:
                self.sleep(self.http_cfg.backoff_factor * 2 ** (attempt - 1))
                continue
            # 400/401/403/404: retrying will not help (bad token, chat id, or HTML).
            raise NotifierError(f"Telegram rejected the message: {redact(last_error)}")
        raise NotifierError(f"Telegram failed after {self.max_attempts} attempts: {redact(last_error)}")
