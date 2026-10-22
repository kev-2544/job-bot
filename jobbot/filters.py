"""Decide which jobs a Kenya-based candidate can realistically apply for.

Pipeline for one job (first decisive rule wins):

1. Title contains a title_exclude keyword         -> reject
2. No role keyword in title, and fewer than
   description_min_keyword_hits in description    -> reject
3. Exclusion phrase/pattern in location or text   -> reject  (beats allow keywords)
4. Allow keyword in the location field            -> keep  ("South Africa" etc. masked first)
5. Restricted region in the location field        -> reject  (e.g. "USA", "Europe")
6. Strong allow keyword in the description        -> keep
7. Only a weak keyword ("global") in description  -> keep, needs_checking
8. Nothing either way                             -> keep, needs_checking (or drop if keep_ambiguous is false)

The optional LLM check (jobbot.llm_check) runs after this on the kept jobs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from .config import FilterConfig
from .models import Job, html_to_text

log = logging.getLogger(__name__)


@dataclass
class FilterResult:
    keep: bool
    needs_checking: bool
    reason: str


def _term_regex(term: str) -> str:
    """Whole-word, whitespace/hyphen-tolerant regex for a plain phrase."""
    words = [re.escape(w) for w in re.split(r"[\s\-]+", term.strip().lower()) if w]
    body = r"[\s\-]+".join(words)
    return rf"(?<![a-z0-9]){body}(?![a-z0-9])"


class TermMatcher:
    """Finds which of a list of plain phrases occur in a text."""

    def __init__(self, terms: Iterable[str]) -> None:
        self.terms = [t.lower() for t in terms if t.strip()]
        self._compiled = [(t, re.compile(_term_regex(t))) for t in self.terms]

    def first(self, text: str) -> str | None:
        for term, rx in self._compiled:
            if rx.search(text):
                return term
        return None

    def all(self, text: str) -> list[str]:
        return [term for term, rx in self._compiled if rx.search(text)]

    def distinct_count(self, text: str) -> int:
        """Number of different terms found; "full stack" and "full-stack" count once."""
        return len({re.sub(r"[\s\-]+", "", t) for t in self.all(text)})


def _prepare(text: str) -> str:
    if "<" in text and ">" in text:
        text = html_to_text(text)
    return re.sub(r"\s+", " ", text).lower()


class JobFilter:
    def __init__(self, cfg: FilterConfig) -> None:
        self.cfg = cfg
        self.role = TermMatcher(cfg.role_keywords)
        self.title_exclude = TermMatcher(cfg.title_exclude_keywords)
        self.allow = TermMatcher(cfg.allow_keywords)
        weak = {w.lower() for w in cfg.weak_description_keywords}
        self.strong_allow = TermMatcher([t for t in cfg.allow_keywords if t.lower() not in weak])
        self.exclude = TermMatcher(cfg.exclude_phrases)
        self.exclude_patterns = [re.compile(p, re.IGNORECASE) for p in cfg.exclude_patterns]
        self.restricted = TermMatcher(cfg.restricted_location_terms)
        self.mask = [re.compile(_term_regex(p)) for p in cfg.allow_mask_phrases if p.strip()]

    def _masked(self, text: str) -> str:
        for rx in self.mask:
            text = rx.sub(" ", text)
        return text

    def evaluate(self, job: Job) -> FilterResult:
        title = _prepare(job.title)
        location = _prepare(job.location_text)
        description = _prepare(job.description)

        # 1-2. Role match
        hit = self.title_exclude.first(title)
        if hit:
            return FilterResult(False, False, f"title excluded ({hit!r})")
        if not self.role.first(title):
            hits = self.role.distinct_count(description) if self.cfg.match_description else 0
            if hits < self.cfg.description_min_keyword_hits:
                return FilterResult(False, False, "no role keyword")

        # 3. Exclusions (location field and description)
        for text in (location, description):
            hit = self.exclude.first(text)
            if hit:
                return FilterResult(False, False, f"excluded phrase ({hit!r})")
            for rx in self.exclude_patterns:
                m = rx.search(text)
                if m:
                    return FilterResult(False, False, f"excluded pattern ({m.group(0)[:60]!r})")

        # 4. Location field says it is open to us
        hit = self.allow.first(self._masked(location))
        if hit:
            return FilterResult(True, False, f"location mentions {hit!r}")

        # 5. Location field names some other region only
        hit = self.restricted.first(location)
        if hit:
            return FilterResult(False, False, f"location restricted to {job.location_text!r}")

        # 6-7. Description
        masked_description = self._masked(description)
        hit = self.strong_allow.first(masked_description)
        if hit:
            return FilterResult(True, False, f"description mentions {hit!r}")
        hit = self.allow.first(masked_description)
        if hit:
            return FilterResult(True, True, f"only a generic mention of {hit!r} in the description")

        # 8. Ambiguous
        reason = "no location information" if not location.strip() or location.strip() == "remote" else (
            f"unclear location {job.location_text!r}"
        )
        if self.cfg.keep_ambiguous:
            return FilterResult(True, True, reason)
        return FilterResult(False, False, f"ambiguous dropped: {reason}")

    def apply(self, jobs: Iterable[Job]) -> list[Job]:
        """Return the jobs that pass, with needs_checking/check_reason set."""
        kept: list[Job] = []
        total = 0
        for job in jobs:
            total += 1
            result = self.evaluate(job)
            if result.keep:
                job.needs_checking = result.needs_checking
                job.check_reason = result.reason if result.needs_checking else ""
                kept.append(job)
                log.debug("KEEP  [%s] %s @ %s: %s", job.source, job.title, job.company, result.reason)
            else:
                log.debug("DROP  [%s] %s @ %s: %s", job.source, job.title, job.company, result.reason)
        flagged = sum(j.needs_checking for j in kept)
        log.info("Filters kept %d of %d job(s) (%d need checking)", len(kept), total, flagged)
        return kept


def filter_jobs(jobs: Iterable[Job], cfg: FilterConfig, storage=None, checker=None) -> list[Job]:
    """Rule-based filtering, then the optional LLM check when cfg.llm.enabled.

    `storage` (jobbot.storage.Storage) caches LLM verdicts; `checker` overrides the
    LLM client (used by tests). Both are only needed when the LLM check is enabled.
    """
    kept = JobFilter(cfg).apply(jobs)
    if cfg.llm.enabled:
        from .llm_check import make_checker, run_llm_checks

        checker = checker or make_checker(cfg.llm)
        if checker is not None:
            kept = run_llm_checks(kept, cfg.llm, checker, storage)
    return kept
