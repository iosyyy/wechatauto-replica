from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def make_plugin():
    def factory(config=None):
        value = dict(config or {})
        return SimpleNamespace(get_config=lambda: value), None
    return factory
