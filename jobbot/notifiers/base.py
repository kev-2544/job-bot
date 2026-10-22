"""Common interface and formatting helpers for notification channels."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar

from ..config import get_env
from ..models import Job

log = logging.getLogger(__name__)

# Display names; sources' terms (Remotive, Remote OK) ask us to name them.
SOURCE_LABELS = {
    "remotive": "Remotive",
    "remoteok": "Remote OK",
    "weworkremotely": "We Work Remotely",
    "jooble": "Jooble",
    "adzuna": "Adzuna",
}


class NotifierError(RuntimeError):
    """A channel failed to deliver. The message must not contain secrets."""


class Notifier(ABC):
    name: ClassVar[str] = ""
    required_env: ClassVar[tuple[str, ...]] = ()

    def missing_env(self) -> list[str]:
        return [n for n in self.required_env if not get_env(n)]

    @abstractmethod
    def send(self, jobs: list[Job]) -> None:
        """Deliver these jobs. Raise NotifierError (or anything) on failure."""

    @abstractmethod
    def send_empty_notice(self) -> None:
        """Tell the user the run found nothing new."""


def source_label(name: str) -> str:
    return SOURCE_LABELS.get(name, name.title() if name else "Unknown source")


def format_date(value: datetime | None) -> str:
    return value.strftime("%d %b %Y") if value else ""


def location_label(job: Job) -> str:
    return job.location_text or "Location not stated"


def subject_line(count: int) -> str:
    plural = "job" if count == 1 else "jobs"
    return f"{count} new Kenya-eligible remote software {plural}"


EMPTY_NOTICE = "No new Kenya-eligible remote software jobs this run."
