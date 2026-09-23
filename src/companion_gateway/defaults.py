"""Packaged defaults for non-emotional runtime settings.

The authoritative default values for host, port, memory, evergreen, upstream,
and proactive scheduling live in defaults.json. Dataclass field defaults in
config.py read from this module so no default value is duplicated in Python.
"""

from __future__ import annotations

import copy

from .resources import load_packaged

_DEFAULTS = load_packaged("defaults.json")


def all_defaults() -> dict:
    return copy.deepcopy(_DEFAULTS)


def section(name: str) -> dict:
    return copy.deepcopy(_DEFAULTS[name])