"""Central logging configuration."""

from __future__ import annotations

import logging
import os
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str | None = None) -> None:
    """Configure root logging to stderr. $LOG_LEVEL overrides the argument."""
    chosen = (os.environ.get("LOG_LEVEL") or level or "INFO").upper()
    numeric = getattr(logging, chosen, None)
    if not isinstance(numeric, int):
        numeric = logging.INFO

    root = logging.getLogger()
    root.setLevel(numeric)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)

    # Third-party libraries are noisy at DEBUG/INFO.
    for noisy in ("urllib3", "httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(max(numeric, logging.WARNING))
