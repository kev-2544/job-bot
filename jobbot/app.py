"""The run pipeline: fetch -> filter -> dedupe -> keep only new -> notify -> store."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TextIO

from .config import Config, ConfigError, load_config, load_dotenv
from .filters import filter_jobs
from .http import HttpClient
from .logging_setup import setup_logging
from .models import Job
from .notifiers import Notifier, build_notifiers, notify_all
from .notifiers.base import format_date, location_label, source_label
from .sources import build_sources, discover_sources, fetch_all
from .storage import Storage, dedupe_batch

log = logging.getLogger("jobbot")

EXIT_OK = 0
EXIT_FAILURE = 1  # every source failed, or every notification channel failed
EXIT_USAGE = 2


@dataclass
class RunResult:
    fetched: int = 0
    kept: int = 0
    new: list[Job] = field(default_factory=list)
    deferred: int = 0  # new jobs beyond max_jobs_per_run, left for the next run
    source_stats: dict[str, int | None] = field(default_factory=dict)
    notify_results: dict[str, bool] = field(default_factory=dict)
    saved: int = 0

    @property
    def all_sources_failed(self) -> bool:
        return bool(self.source_stats) and all(v is None for v in self.source_stats.values())

    @property
    def all_channels_failed(self) -> bool:
        return bool(self.notify_results) and not any(self.notify_results.values())


def sort_jobs(jobs: list[Job]) -> list[Job]:
    """Confirmed jobs first, then needs-checking; newest first within each group."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(jobs, key=lambda j: (j.needs_checking, -(j.posted_date or epoch).timestamp()))


def run(
    config: Config,
    dry_run: bool = False,
    only_source: str | None = None,
    notifiers: list[Notifier] | None = None,
    http: HttpClient | None = None,
    seed: bool = False,
) -> RunResult:
    """One bot run. `seed` marks every new job as seen without sending anything
    (use once on a fresh deployment to skip the backlog)."""
    if seed and dry_run:
        raise ValueError("--seed and --dry-run cannot be combined")
    result = RunResult()
    http = http or HttpClient(config.http)

    sources = build_sources(config, http, only=only_source)
    if not sources:
        log.error("No sources to run (all disabled or missing API keys)")
    raw, result.source_stats = fetch_all(sources)
    result.fetched = len(raw)

    with Storage(config.storage.db_path, read_only=dry_run) as store:
        kept = filter_jobs(raw, config.filters, storage=store)
        result.kept = len(kept)
        unique = dedupe_batch(kept)
        new = sort_jobs(store.filter_new(unique))
        log.info(
            "%d fetched, %d passed filters, %d after dedupe, %d new (%d already seen in the database)",
            len(raw), len(kept), len(unique), len(new), store.seen_count(),
        )

        if seed:
            result.new = new
            result.saved = store.mark_seen(new)
            log.info("Seed: saved %d job(s) as seen without sending", result.saved)
            return result

        cap = config.notifications.max_jobs_per_run
        if cap > 0 and len(new) > cap:
            result.deferred = len(new) - cap
            log.warning("%d new jobs exceed max_jobs_per_run=%d; the rest wait for the next run", len(new), cap)
            new = new[:cap]
        result.new = new

        if dry_run:
            log.info("Dry run: nothing sent, nothing saved")
            return result

        notifiers = build_notifiers(config) if notifiers is None else notifiers
        result.notify_results = notify_all(notifiers, new, config.notifications.send_empty_notice)

        if not new:
            return result
        if any(result.notify_results.values()):
            result.saved = store.mark_seen(new)
            log.info("Saved %d job(s) as seen", result.saved)
        else:
            # Not saved, so the next run tries to deliver them again.
            log.error("No notification channel delivered; jobs NOT saved and will be retried next run")
    return result


def print_jobs(jobs: list[Job], out: TextIO = sys.stdout) -> None:
    """Human-readable listing for --dry-run (this is program output, not logging)."""
    if not jobs:
        out.write("No new jobs.\n")
        return
    for i, job in enumerate(jobs, start=1):
        flag = "  [NEEDS CHECKING: " + (job.check_reason or "location unclear") + "]" if job.needs_checking else ""
        posted = f", posted {format_date(job.posted_date)}" if job.posted_date else ""
        out.write(f"{i:>3}. {job.title} - {job.company}{flag}\n")
        out.write(f"     {location_label(job)} | {source_label(job.source)}{posted}\n")
        out.write(f"     {job.url}\n")
    flagged = sum(j.needs_checking for j in jobs)
    out.write(f"\n{len(jobs)} new job(s), {flagged} need checking.\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remote software job alerts for Kenya-based candidates.")
    parser.add_argument("--dry-run", action="store_true", help="print new jobs instead of sending them or saving to the database")
    parser.add_argument("--source", metavar="NAME", help="run only this source (works even if it is disabled in config)")
    parser.add_argument("--config", metavar="PATH", help="config file (default: $JOBBOT_CONFIG or config.yaml)")
    parser.add_argument("--seed", action="store_true", help="mark all current jobs as seen without sending (first deployment)")
    parser.add_argument("--list-sources", action="store_true", help="list available sources and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging (shows why each job was kept/dropped)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        # Job titles contain any Unicode; never crash on a legacy console encoding.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    load_dotenv()
    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as exc:
        setup_logging("INFO")
        log.error("Invalid configuration: %s", exc)
        return EXIT_USAGE
    setup_logging("DEBUG" if args.verbose else config.log_level)

    if args.list_sources:
        for name in discover_sources():
            state = "enabled" if config.source_enabled(name) else "disabled"
            sys.stdout.write(f"{name} ({state})\n")
        return EXIT_OK

    if args.seed and args.dry_run:
        log.error("--seed and --dry-run cannot be combined")
        return EXIT_USAGE
    try:
        result = run(config, dry_run=args.dry_run, only_source=args.source, seed=args.seed)
    except KeyError as exc:
        log.error("%s", exc.args[0])
        return EXIT_USAGE

    if args.dry_run:
        print_jobs(result.new)
    if result.all_sources_failed:
        log.error("Every source failed")
        return EXIT_FAILURE
    if result.all_channels_failed:
        return EXIT_FAILURE
    return EXIT_OK
