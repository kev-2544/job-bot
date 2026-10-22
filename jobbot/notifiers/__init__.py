"""Notification channels. Each channel is isolated: one failing never stops another."""

from __future__ import annotations

import logging

from ..config import Config
from ..models import Job
from .base import Notifier, NotifierError
from .emailer import EmailNotifier
from .telegram import TelegramNotifier

log = logging.getLogger(__name__)


def build_notifiers(config: Config) -> list[Notifier]:
    """The channels enabled in config.yaml (notifications.telegram / notifications.email)."""
    notifiers: list[Notifier] = []
    if config.notifications.telegram:
        notifiers.append(TelegramNotifier(config.http, digest_threshold=config.notifications.telegram_digest_threshold))
    else:
        log.info("Telegram notifications disabled in config")
    if config.notifications.email:
        notifiers.append(EmailNotifier(timeout=max(config.http.timeout_seconds, 30)))
    else:
        log.info("Email notifications disabled in config")
    return notifiers


def notify_all(notifiers: list[Notifier], jobs: list[Job], send_empty_notice: bool = False) -> dict[str, bool]:
    """Send `jobs` through every channel. Returns {channel: delivered}.

    With no jobs, nothing is sent (and {} is returned) unless send_empty_notice.
    An enabled channel with missing env vars counts as a failure.
    """
    if not jobs and not send_empty_notice:
        log.info("No new jobs; nothing to send")
        return {}

    results: dict[str, bool] = {}
    for notifier in notifiers:
        missing = notifier.missing_env()
        if missing:
            log.error("%s is enabled but missing environment variable(s): %s", notifier.name, ", ".join(missing))
            results[notifier.name] = False
            continue
        try:
            if jobs:
                notifier.send(jobs)
            else:
                notifier.send_empty_notice()
            results[notifier.name] = True
        except Exception as exc:  # noqa: BLE001 - isolate channels from each other
            log.error("%s notification failed: %s", notifier.name, exc)
            results[notifier.name] = False
    return results


__all__ = ["Notifier", "NotifierError", "EmailNotifier", "TelegramNotifier", "build_notifiers", "notify_all"]
