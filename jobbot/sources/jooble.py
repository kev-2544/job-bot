"""Jooble REST API (free key from https://jooble.org/api/about). Needs JOOBLE_API_KEY."""

from __future__ import annotations

from ..models import Job, html_to_text
from .base import Source, parse_date


class JoobleSource(Source):
    name = "jooble"
    required_env = ("JOOBLE_API_KEY",)

    def fetch(self) -> list[Job]:
        url = f"https://jooble.org/api/{self.env('JOOBLE_API_KEY')}"
        payload = {
            "keywords": self.setting("keywords", "software developer"),
            "location": self.setting("location", "Kenya"),
            "page": "1",
        }
        data = self.http.post_json(url, payload)
        items = data.get("jobs", []) if isinstance(data, dict) else []

        jobs: list[Job] = []
        for item in items:
            if not item.get("link") or not item.get("title"):
                continue
            jobs.append(
                Job(
                    title=html_to_text(item["title"]),
                    company=item.get("company", ""),
                    url=item["link"],
                    location_text=item.get("location", ""),
                    description=html_to_text(item.get("snippet", "")),
                    source=self.name,
                    posted_date=parse_date(item.get("updated")),
                )
            )
        return jobs
