"""Load config.yaml (merged over built-in defaults) and environment secrets."""

from __future__ import annotations

import copy
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.yaml"


class ConfigError(ValueError):
    """Raised when config.yaml contains an invalid value."""


# Built-in defaults. config.yaml is deep-merged on top: dict keys merge,
# lists and scalars in config.yaml REPLACE the default value.
DEFAULTS: dict[str, Any] = {
    "log_level": "INFO",
    "storage": {"db_path": "data/jobs.db"},
    "http": {
        "timeout_seconds": 20,
        "max_retries": 3,
        "backoff_factor": 2,
        "user_agent": "kenya-remote-job-bot/1.0 (personal job alerts)",
    },
    "sources": {
        "remotive": {
            "enabled": True,
            "url": "https://remotive.com/api/remote-jobs",
            "category": "software-dev",
            "categories": ["Software Development", "DevOps / Sysadmin"],
        },
        "remoteok": {"enabled": True, "url": "https://remoteok.com/api"},
        "weworkremotely": {
            "enabled": True,
            "feeds": [
                "https://weworkremotely.com/categories/remote-programming-jobs.rss",
                "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
                "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
                "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
                "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
            ],
        },
        "jooble": {"enabled": True, "keywords": "software developer", "location": "Kenya"},
        "adzuna": {
            "enabled": True,
            "countries": ["gb", "us"],
            "what": "software developer remote",
            "results_per_page": 50,
        },
    },
    "filters": {
        "role_keywords": [
            "software", "developer", "engineer", "backend", "back-end", "back end",
            "frontend", "front-end", "front end", "full stack", "full-stack", "fullstack",
            "python", "javascript", "typescript", "devops", "mobile", "android", "ios", "sre",
        ],
        "match_description": True,
        "description_min_keyword_hits": 3,
        "title_exclude_keywords": ["sales", "recruiter", "recruiting", "marketing", "account executive"],
        "allow_keywords": [
            "kenya", "nairobi", "africa", "emea", "worldwide", "anywhere", "global", "work from anywhere",
        ],
        "weak_description_keywords": ["worldwide", "anywhere", "global"],
        "allow_mask_phrases": ["south africa", "north africa", "west africa"],
        "exclude_phrases": [
            "us only", "usa only", "u.s. only", "united states only",
            "must be located in the us", "must be located in the united states",
            "must be based in the us", "must reside in the us", "us residents", "us citizens only",
            "uk only", "uk residents", "must be based in the uk",
            "eu only", "europe only", "eu residents",
            "north america only", "americas only", "canada only", "latam only", "apac only",
            "work authorization required",
        ],
        "exclude_patterns": [
            r"no (visa )?sponsorship[^.]{0,80}\b(u\.s\.?|usa|united states|uk|u\.k\.|united kingdom|canada|eu|europe|germany|australia)\b",
            r"\b(u\.s\.?|usa|united states|uk|u\.k\.|united kingdom|canada|eu|europe|germany|australia)\b[^.]{0,80}no (visa )?sponsorship",
            r"(authori[sz]ed|eligible|legally able) to work in the (u\.s\.?|usa|united states|uk|united kingdom|canada|eu|european union)",
            r"must (be|live|reside)( located| based)? in (the )?(us|u\.s\.?|usa|united states|uk|united kingdom|canada|europe|eu)\b",
            r"\b(anywhere|remote) (with)?in (the )?(us|u\.s\.?|usa|united states|uk|united kingdom|canada|europe|eu)\b",
        ],
        "restricted_location_terms": [
            "us", "usa", "u.s.", "united states", "america", "americas", "north america",
            "latam", "latin america", "canada", "uk", "united kingdom", "europe", "eu",
            "germany", "france", "spain", "netherlands", "poland", "portugal", "ireland",
            "india", "apac", "asia", "australia", "brazil", "mexico", "philippines",
            "south africa", "north africa", "west africa", "nigeria", "egypt", "ghana", "morocco",
        ],
        "keep_ambiguous": True,
        "llm": {
            "enabled": False,
            "model": "claude-sonnet-4-6",
            "scope": "ambiguous",
            "max_checks_per_run": 30,
            "max_description_chars": 8000,
            "fail_open": True,
        },
    },
    "notifications": {
        "telegram": True,
        "email": True,
        "send_empty_notice": False,
        "telegram_digest_threshold": 10,
        "max_jobs_per_run": 50,
    },
}


@dataclass
class HttpConfig:
    timeout_seconds: float
    max_retries: int
    backoff_factor: float
    user_agent: str


@dataclass
class StorageConfig:
    db_path: str


@dataclass
class LLMConfig:
    enabled: bool
    model: str
    scope: str  # "ambiguous" | "all"
    max_checks_per_run: int
    max_description_chars: int
    fail_open: bool


@dataclass
class FilterConfig:
    role_keywords: list[str]
    match_description: bool
    description_min_keyword_hits: int
    title_exclude_keywords: list[str]
    allow_keywords: list[str]
    weak_description_keywords: list[str]
    allow_mask_phrases: list[str]
    exclude_phrases: list[str]
    exclude_patterns: list[str]
    restricted_location_terms: list[str]
    keep_ambiguous: bool
    llm: LLMConfig


@dataclass
class NotificationConfig:
    telegram: bool
    email: bool
    send_empty_notice: bool
    telegram_digest_threshold: int
    max_jobs_per_run: int


@dataclass
class Config:
    log_level: str
    storage: StorageConfig
    http: HttpConfig
    filters: FilterConfig
    notifications: NotificationConfig
    # Per-source settings keyed by source name, e.g. sources["remotive"]["enabled"].
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)

    def source_settings(self, name: str) -> dict[str, Any]:
        return self.sources.get(name, {})

    def source_enabled(self, name: str) -> bool:
        return bool(self.source_settings(name).get("enabled", True))


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into a copy of base (dicts merge, everything else replaces)."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _str_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list, got {type(value).__name__}")
    return [str(v).strip() for v in value if str(v).strip()]


def build_config(data: dict[str, Any]) -> Config:
    """Turn a merged config dict into a validated Config object."""
    f = data["filters"]
    llm = f["llm"]
    scope = str(llm.get("scope", "ambiguous")).lower()
    if scope not in ("ambiguous", "all"):
        raise ConfigError(f"filters.llm.scope must be 'ambiguous' or 'all', got {scope!r}")

    patterns = _str_list(f.get("exclude_patterns"), "filters.exclude_patterns")
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"Invalid regex in filters.exclude_patterns: {pattern!r}: {exc}") from exc

    sources = data.get("sources") or {}
    if not isinstance(sources, dict):
        raise ConfigError("sources must be a mapping of source name -> settings")

    n = data["notifications"]
    h = data["http"]
    return Config(
        log_level=str(data.get("log_level", "INFO")).upper(),
        storage=StorageConfig(db_path=str(data["storage"]["db_path"])),
        http=HttpConfig(
            timeout_seconds=float(h["timeout_seconds"]),
            max_retries=int(h["max_retries"]),
            backoff_factor=float(h["backoff_factor"]),
            user_agent=str(h["user_agent"]),
        ),
        filters=FilterConfig(
            role_keywords=_str_list(f.get("role_keywords"), "filters.role_keywords"),
            match_description=bool(f.get("match_description", True)),
            description_min_keyword_hits=max(1, int(f.get("description_min_keyword_hits", 3))),
            title_exclude_keywords=_str_list(f.get("title_exclude_keywords"), "filters.title_exclude_keywords"),
            allow_keywords=_str_list(f.get("allow_keywords"), "filters.allow_keywords"),
            weak_description_keywords=_str_list(
                f.get("weak_description_keywords"), "filters.weak_description_keywords"
            ),
            allow_mask_phrases=_str_list(f.get("allow_mask_phrases"), "filters.allow_mask_phrases"),
            exclude_phrases=_str_list(f.get("exclude_phrases"), "filters.exclude_phrases"),
            exclude_patterns=patterns,
            restricted_location_terms=_str_list(
                f.get("restricted_location_terms"), "filters.restricted_location_terms"
            ),
            keep_ambiguous=bool(f.get("keep_ambiguous", True)),
            llm=LLMConfig(
                enabled=bool(llm.get("enabled", False)),
                model=str(llm.get("model", "claude-sonnet-4-6")),
                scope=scope,
                max_checks_per_run=int(llm.get("max_checks_per_run", 30)),
                max_description_chars=int(llm.get("max_description_chars", 8000)),
                fail_open=bool(llm.get("fail_open", True)),
            ),
        ),
        notifications=NotificationConfig(
            telegram=bool(n["telegram"]),
            email=bool(n["email"]),
            send_empty_notice=bool(n["send_empty_notice"]),
            telegram_digest_threshold=int(n["telegram_digest_threshold"]),
            max_jobs_per_run=int(n["max_jobs_per_run"]),
        ),
        sources={str(k): dict(v or {}) for k, v in sources.items()},
    )


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config from `path` (or $JOBBOT_CONFIG, or ./config.yaml) over DEFAULTS.

    A missing file is not an error: the built-in defaults are used.
    """
    path = Path(path or os.environ.get("JOBBOT_CONFIG") or DEFAULT_CONFIG_PATH)
    user_data: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a YAML mapping at the top level")
        user_data = loaded
        log.debug("Loaded config from %s", path)
    else:
        log.warning("Config file %s not found; using built-in defaults", path)
    return build_config(deep_merge(DEFAULTS, user_data))


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Minimal .env loader for local runs. Never overrides variables already set."""
    path = Path(path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def get_env(name: str, default: str | None = None) -> str | None:
    """Read an environment variable, treating blank values as unset."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()
