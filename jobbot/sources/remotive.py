"""Remotive public API. Terms: link back to the Remotive URL, name Remotive as the
source, and call at most ~4 times a day (we run twice)."""

from __future__ import annotations

from ..models import Job, html_to_text
from .base import Source, parse_date


class RemotiveSource(Source):
    name = "remotive"

    def fetch(self) -> list[Job]:
        url = self.setting("url", "https://remotive.com/api/remote-jobs")
        params = {}
        if self.setting("category"):
            params["category"] = self.setting("category")
        data = self.http.get_json(url, params=params)
        items = data.get("jobs", []) if isinstance(data, dict) else []

        wanted = {c.strip().lower() for c in self.setting("categories") or []}
        jobs: list[Job] = []
        for item in items:
            if not item.get("url") or not item.get("title"):
                continue
            if wanted and str(item.get("category", "")).strip().lower() not in wanted:
                continue
            tags = [str(t) for t in item.get("tags") or []]
            jobs.append(
                Job(
                    title=item["title"],
                    company=item.get("company_name", ""),
                    url=item["url"],
                    location_text=item.get("candidate_required_location", ""),
                    description=html_to_text(item.get("description", "")),
                    source=self.name,
                    posted_date=parse_date(item.get("publication_date")),
                    tags=tags,
                )
            )
        return jobs
