"""Normalized job record shared by every source, filter, store and notifier."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only track where a click came from; they must not
# change a job's identity.
_TRACKING_PARAMS = re.compile(r"^(utm_.*|ref|referrer|source|src|fbclid|gclid)$", re.I)

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|gmbh|corp|corporation|co|plc|sa|bv|ag|pty)\b\.?",
    re.I,
)
_TITLE_NOISE = re.compile(r"\((?:[^)]*)\)|\[(?:[^\]]*)\]|\b(remote|100% remote|fully remote|wfh)\b", re.I)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def normalize_url(url: str) -> str:
    """Canonical form of a URL: lowercase scheme/host, no fragment, no tracking params."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def make_job_id(url: str) -> str:
    """Stable identifier: SHA-256 of the normalized URL (first 20 hex chars)."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:20]


def normalize_company(company: str) -> str:
    text = _COMPANY_SUFFIXES.sub(" ", (company or "").lower())
    return _NON_ALNUM.sub(" ", text).strip()


def normalize_title(title: str) -> str:
    text = _TITLE_NOISE.sub(" ", (title or "").lower())
    text = text.replace("sr.", "senior").replace("jr.", "junior")
    text = re.sub(r"\bsr\b", "senior", text)
    text = re.sub(r"\bjr\b", "junior", text)
    return _NON_ALNUM.sub(" ", text).strip()


def make_dedupe_key(company: str, title: str) -> str:
    """Cross-source duplicate key: normalized company + '|' + normalized title."""
    return f"{normalize_company(company)}|{normalize_title(title)}"


def html_to_text(raw: str) -> str:
    """Rough HTML -> plain text, good enough for keyword matching."""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", raw or "")
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


@dataclass
class Job:
    title: str
    company: str
    url: str
    location_text: str = ""
    description: str = ""
    source: str = ""
    posted_date: datetime | None = None
    job_id: str = ""
    needs_checking: bool = False
    # Human-readable explanation from the filter (e.g. why it needs checking).
    check_reason: str = ""
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.title = (self.title or "").strip()
        self.company = (self.company or "").strip() or "Unknown company"
        self.url = (self.url or "").strip()
        self.location_text = (self.location_text or "").strip()
        self.description = self.description or ""
        if not self.job_id:
            self.job_id = make_job_id(self.url)

    @property
    def dedupe_key(self) -> str:
        return make_dedupe_key(self.company, self.title)
