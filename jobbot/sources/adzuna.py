"""Adzuna search API (free keys from https://developer.adzuna.com). Needs ADZUNA_APP_ID
and ADZUNA_APP_KEY.

Adzuna has no Kenya market, so we search the markets in `countries` for remote
roles and let the location filter decide. Adzuna's location area (e.g. ["UK",
"London"]) goes into location_text, so jobs tied to those countries are
rejected unless the posting says it is open worldwide/Africa.
"""

from __future__ import annotations

from ..models import Job, html_to_text
from .base import Source, parse_date

API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"


class AdzunaSource(Source):
    name = "adzuna"
    required_env = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY")

    def fetch(self) -> list[Job]:
        countries = self.setting("countries") or ["gb"]
        jobs: list[Job] = []
        failures = 0
        for country in countries:
            try:
                jobs.extend(self._search(str(country).lower()))
            except Exception as exc:  # noqa: BLE001 - keep other markets
                failures += 1
                self.log.warning("Adzuna %s search failed: %s", country, exc)
        if failures == len(countries):
            raise RuntimeError(f"all {failures} Adzuna searches failed")
        return jobs

    def _search(self, country: str) -> list[Job]:
        params = {
            "app_id": self.env("ADZUNA_APP_ID"),
            "app_key": self.env("ADZUNA_APP_KEY"),
            "what": self.setting("what", "software developer remote"),
            "results_per_page": int(self.setting("results_per_page", 50)),
            "content-type": "application/json",
        }
        data = self.http.get_json(API_URL.format(country=country), params=params)
        items = data.get("results", []) if isinstance(data, dict) else []

        jobs: list[Job] = []
        for item in items:
            link = item.get("redirect_url")
            if not link or not item.get("title"):
                continue
            location = item.get("location") or {}
            parts = [location.get("display_name", "")] + list(location.get("area") or [])
            location_text = ", ".join(dict.fromkeys(p for p in parts if p))
            jobs.append(
                Job(
                    title=html_to_text(item["title"]),
                    company=(item.get("company") or {}).get("display_name", ""),
                    url=link,
                    location_text=location_text,
                    description=html_to_text(item.get("description", "")),
                    source=self.name,
                    posted_date=parse_date(item.get("created")),
                )
            )
        return jobs
