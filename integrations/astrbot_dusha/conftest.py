from __future__ import annotations

import importlib.util
from pathlib import Path

_SUITE = Path(__file__).resolve().parents[2] / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("dusha_test_fixtures", _SUITE)
assert _spec and _spec.loader
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)

config = _fixtures.config
