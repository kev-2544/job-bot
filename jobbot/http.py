"""Shared HTTP client: polite User-Agent, timeouts, retries with exponential backoff."""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import HttpConfig, get_env

log = logging.getLogger(__name__)

RETRY_STATUSES = (429, 500, 502, 503, 504)

# Secrets that can appear inside request URLs (path or query string). Their
# values are scrubbed from error messages before anything is logged.
SECRET_ENV_VARS = ("JOOBLE_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "TELEGRAM_BOT_TOKEN")


class HttpError(RuntimeError):
    """A failed HTTP call, with secrets removed from the message."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def redact(text: str) -> str:
    for name in SECRET_ENV_VARS:
        value = get_env(name)
        if value:
            text = text.replace(value, f"<{name}>")
    return text


def make_session(cfg: HttpConfig, retry_statuses: tuple[int, ...] = RETRY_STATUSES) -> requests.Session:
    """A requests.Session that retries connection errors and `retry_statuses`.

    urllib3 sleeps backoff_factor * 2**(n-1) seconds between attempts (capped at
    60s) and honours a Retry-After header on 429/503.
    """
    retry = Retry(
        total=cfg.max_retries,
        connect=cfg.max_retries,
        read=cfg.max_retries,
        status=cfg.max_retries,
        backoff_factor=cfg.backoff_factor,
        backoff_max=60,
        status_forcelist=retry_statuses,
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": cfg.user_agent, "Accept": "application/json, */*;q=0.8"})
    return session


class HttpClient:
    """Small wrapper so every call gets the configured timeout and raises on HTTP errors."""

    def __init__(self, cfg: HttpConfig, session: requests.Session | None = None) -> None:
        self.cfg = cfg
        self.session = session or make_session(cfg)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.cfg.timeout_seconds)
        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            raise HttpError(redact(str(exc)), status) from None
        except requests.RequestException as exc:
            raise HttpError(redact(f"{type(exc).__name__}: {exc}")) from None
        return response

    @staticmethod
    def _json(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise HttpError(f"Invalid JSON from {redact(response.url)}") from None

    def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return self._json(self.request("GET", url, params=params, **kwargs))

    def post_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        return self._json(self.request("POST", url, json=payload, **kwargs))

    def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        return self.request("GET", url, **kwargs).content
