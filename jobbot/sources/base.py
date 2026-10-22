"""Common interface for job sources.

To add a source, create one file in jobbot/sources/ containing a Source
subclass with a unique `name` and a `fetch()` that returns normalized Jobs.
It is discovered automatically; settings come from config.yaml sources.<name>.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

from ..config import get_env
from ..http import HttpClient
from ..models import Job

log = logging.getLogger(__name__)


class Source(ABC):
    #: Unique key used in config.yaml (sources.<name>), --source and Job.source.
    name: ClassVar[str] = ""
    #: Environment variables that must be set; if any is missing the source is skipped.
    required_env: ClassVar[tuple[str, ...]] = ()

    def __init__(self, settings: dict[str, Any], http: HttpClient) -> None:
        self.settings = settings or {}
        self.http = http
        self.log = logging.getLogger(f"jobbot.sources.{self.name}")

    def missing_env(self) -> list[str]:
        return [name for name in self.required_env if not get_env(name)]

    def env(self, name: str) -> str:
        value = get_env(name)
        if value is None:
            raise RuntimeError(f"{name} is not set")
        return value

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    @abstractmethod
    def fetch(self) -> list[Job]:
        """Return normalized jobs. Raise on failure; the caller isolates errors."""


def parse_date(value: Any) -> datetime | None:
    """Best-effort parse of ISO-8601 strings, RFC-822 dates (RSS) and Unix epochs to aware UTC."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        text = str(value).strip()
        if text.isdigit():
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, IndexError):
        log.debug("Could not parse date %r", value)
        return None
