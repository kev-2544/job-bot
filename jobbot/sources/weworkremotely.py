"""We Work Remotely category RSS feeds. Item titles look like "Company: Job Title"."""

from __future__ import annotations

import feedparser

from ..models import Job, html_to_text
from .base import Source, parse_date


class WeWorkRemotelySource(Source):
    name = "weworkremotely"

    def fetch(self) -> list[Job]:
        feeds = self.setting("feeds") or []
        jobs: list[Job] = []
        failures = 0
        for feed_url in feeds:
            try:
                jobs.extend(self._fetch_feed(feed_url))
            except Exception as exc:  # noqa: BLE001 - one broken feed must not lose the others
                failures += 1
                self.log.warning("Feed %s failed: %s", feed_url, exc)
        if feeds and failures == len(feeds):
            raise RuntimeError(f"all {failures} We Work Remotely feeds failed")
        return jobs

    def _fetch_feed(self, feed_url: str) -> list[Job]:
        # Download with our session (timeouts, retries, User-Agent), parse locally.
        parsed = feedparser.parse(self.http.get_bytes(feed_url))
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"unparseable feed: {parsed.get('bozo_exception')}")

        jobs: list[Job] = []
        for entry in parsed.entries:
            link = entry.get("link")
            raw_title = entry.get("title", "")
            if not link or not raw_title:
                continue
            company, sep, title = raw_title.partition(":")
            if not sep:
                company, title = "", raw_title
            categories = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]
            jobs.append(
                Job(
                    title=title.strip(),
                    company=company.strip(),
                    url=link,
                    location_text=entry.get("region", ""),
                    description=html_to_text(entry.get("summary", "")),
                    source=self.name,
                    posted_date=parse_date(entry.get("published")),
                    tags=categories,
                )
            )
        return jobs
