"""Remote OK JSON feed. Element 0 is the legal notice. Terms: link back to the
Remote OK job URL and mention Remote OK as the source."""

from __future__ import annotations

from ..models import Job, html_to_text
from .base import Source, parse_date


class RemoteOKSource(Source):
    name = "remoteok"

    def fetch(self) -> list[Job]:
        url = self.setting("url", "https://remoteok.com/api")
        data = self.http.get_json(url)
        if not isinstance(data, list):
            raise ValueError(f"Unexpected Remote OK response type {type(data).__name__}")

        jobs: list[Job] = []
        for item in data[1:]:  # skip the legal notice
            if not isinstance(item, dict):
                continue
            link = item.get("url") or item.get("apply_url")
            title = item.get("position")
            if not link or not title:
                continue
            tags = [str(t) for t in item.get("tags") or []]
            description = html_to_text(item.get("description", ""))
            if tags:
                # Tags carry the tech stack, which helps the role filter.
                description = f"{description}\nTags: {', '.join(tags)}"
            jobs.append(
                Job(
                    title=title,
                    company=item.get("company", ""),
                    url=link,
                    location_text=item.get("location", ""),
                    description=description,
                    source=self.name,
                    posted_date=parse_date(item.get("date") or item.get("epoch")),
                    tags=tags,
                )
            )
        return jobs
