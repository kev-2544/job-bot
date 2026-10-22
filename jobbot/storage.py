"""SQLite persistence: seen jobs (for dedupe across runs) and the LLM result cache."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Job

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    job_id      TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    url         TEXT NOT NULL,
    source      TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_seen_jobs_dedupe_key ON seen_jobs(dedupe_key);

CREATE TABLE IF NOT EXISTS llm_cache (
    job_id      TEXT PRIMARY KEY,
    eligible    TEXT NOT NULL,   -- 'true' | 'false' | 'unclear'
    reason      TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    checked_at  TEXT NOT NULL
);
"""


@dataclass
class LLMVerdict:
    """Result of an LLM eligibility check. eligible is True, False or None (unclear)."""

    eligible: bool | None
    reason: str
    model: str = ""

    @property
    def label(self) -> str:
        return {True: "true", False: "false", None: "unclear"}[self.eligible]

    @staticmethod
    def from_label(label: str, reason: str, model: str = "") -> "LLMVerdict":
        value = {"true": True, "false": False}.get(str(label).strip().lower())
        return LLMVerdict(eligible=value, reason=reason, model=model)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Storage:
    """Thin wrapper around a SQLite file. Use as a context manager or call close().

    With read_only=True (used by --dry-run) every write is skipped, and a missing
    database file is not created: an empty in-memory database is used instead.
    """

    def __init__(self, db_path: str | Path, read_only: bool = False) -> None:
        self.db_path = str(db_path)
        self.read_only = read_only
        target = self.db_path
        if read_only and target != ":memory:" and not Path(target).exists():
            log.info("Database %s does not exist yet; dry run starts from an empty one", target)
            target = ":memory:"
        elif target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(target)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # --- seen jobs -------------------------------------------------------

    def is_seen(self, job: Job) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_jobs WHERE job_id = ? OR (dedupe_key = ? AND dedupe_key != '') LIMIT 1",
            (job.job_id, job.dedupe_key),
        ).fetchone()
        return row is not None

    def filter_new(self, jobs: Iterable[Job]) -> list[Job]:
        """Jobs whose job_id AND company+title key are not yet stored. Read-only."""
        return [job for job in jobs if not self.is_seen(job)]

    def mark_seen(self, jobs: Iterable[Job]) -> int:
        """Insert jobs into seen_jobs (ignoring ones already there). Returns rows inserted."""
        if self.read_only:
            log.debug("Read-only storage: not marking jobs as seen")
            return 0
        now = _now()
        rows = [
            (j.job_id, j.title, j.company, j.url, j.source, now, j.dedupe_key)
            for j in jobs
        ]
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_jobs (job_id, title, company, url, source, first_seen, dedupe_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        inserted = self.conn.total_changes - before
        log.debug("Marked %d job(s) as seen", inserted)
        return inserted

    def seen_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0])

    # --- LLM cache -------------------------------------------------------

    def get_llm_verdict(self, job_id: str) -> LLMVerdict | None:
        row = self.conn.execute(
            "SELECT eligible, reason, model FROM llm_cache WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return LLMVerdict.from_label(row["eligible"], row["reason"], row["model"])

    def save_llm_verdict(self, job_id: str, verdict: LLMVerdict) -> None:
        if self.read_only:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_cache (job_id, eligible, reason, model, checked_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, verdict.label, verdict.reason, verdict.model, _now()),
        )
        self.conn.commit()


def dedupe_batch(jobs: Iterable[Job]) -> list[Job]:
    """Remove duplicates within one run, by job_id and by company+title.

    The first occurrence wins, except that a confirmed job (needs_checking=False)
    replaces an earlier flagged duplicate, so the clearer listing is the one sent.
    """
    kept: list[Job] = []
    index_by_key: dict[str, int] = {}
    for job in jobs:
        keys = [job.job_id, "k:" + job.dedupe_key]
        existing = next((index_by_key[k] for k in keys if k in index_by_key), None)
        if existing is None:
            index_by_key.update({k: len(kept) for k in keys})
            kept.append(job)
            continue
        if kept[existing].needs_checking and not job.needs_checking:
            log.debug("Replacing flagged duplicate %r with confirmed listing from %s", job.title, job.source)
            kept[existing] = job
            index_by_key.update({k: existing for k in keys})
        else:
            log.debug("Dropping duplicate %r from %s", job.title, job.source)
    return kept
