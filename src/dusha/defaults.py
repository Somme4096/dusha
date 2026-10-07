from __future__ import annotations

import copy

from .resources import load_packaged

_DEFAULTS = load_packaged("defaults.json")


def all_defaults() -> dict:
    return copy.deepcopy(_DEFAULTS)