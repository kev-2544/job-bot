import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobbot.config import load_config  # noqa: E402


@pytest.fixture
def config():
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def filter_cfg(config):
    return config.filters
