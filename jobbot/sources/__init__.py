"""Source discovery: every Source subclass in this package is registered by name."""

from __future__ import annotations

import importlib
import logging
import pkgutil

from ..config import Config
from ..http import HttpClient
from ..models import Job
from .base import Source

log = logging.getLogger(__name__)


def discover_sources() -> dict[str, type[Source]]:
    """Import every module in jobbot/sources and collect Source subclasses by name."""
    registry: dict[str, type[Source]] = {}
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_") or module_info.name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, Source) and obj is not Source and obj.name:
                if obj.name in registry and registry[obj.name] is not obj:
                    raise RuntimeError(f"Duplicate source name {obj.name!r}")
                registry[obj.name] = obj
    return registry


def ordered_names(config: Config, registry: dict[str, type[Source]]) -> list[str]:
    """Config order first (it decides which listing wins a duplicate), then any others A-Z."""
    configured = [n for n in config.sources if n in registry]
    return configured + sorted(n for n in registry if n not in configured)


def build_sources(config: Config, http: HttpClient, only: str | None = None) -> list[Source]:
    """Instantiate enabled sources that have their required env vars.

    `only` (from --source) runs just that one, even if it is disabled in config.
    """
    registry = discover_sources()
    if only is not None:
        if only not in registry:
            raise KeyError(f"Unknown source {only!r}. Available: {', '.join(sorted(registry))}")
        names = [only]
    else:
        names = ordered_names(config, registry)

    sources: list[Source] = []
    for name in names:
        if only is None and not config.source_enabled(name):
            log.info("Source %s is disabled in config", name)
            continue
        source = registry[name](config.source_settings(name), http)
        missing = source.missing_env()
        if missing:
            log.warning("Skipping source %s: missing environment variable(s) %s", name, ", ".join(missing))
            continue
        sources.append(source)
    return sources


def fetch_all(sources: list[Source]) -> tuple[list[Job], dict[str, int | None]]:
    """Fetch from every source; a failing source is logged and recorded as None."""
    jobs: list[Job] = []
    stats: dict[str, int | None] = {}
    for source in sources:
        try:
            found = source.fetch()
        except Exception as exc:  # noqa: BLE001 - one broken source must not stop the run
            log.error("Source %s failed: %s", source.name, exc, exc_info=log.isEnabledFor(logging.DEBUG))
            stats[source.name] = None
            continue
        for job in found:
            job.source = job.source or source.name
        log.info("Source %s returned %d job(s)", source.name, len(found))
        stats[source.name] = len(found)
        jobs.extend(found)
    return jobs, stats


__all__ = ["Source", "discover_sources", "build_sources", "fetch_all", "ordered_names"]
