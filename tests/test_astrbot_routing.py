from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).parents[1] / "integrations" / "astrbot_companion_gateway" / "routing.py"
_SPEC = importlib.util.spec_from_file_location("astrbot_gateway_routing", _PATH)
assert _SPEC and _SPEC.loader
routing = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(routing)


def test_platform_selection_is_exact_and_disabled_when_empty():
    assert routing.accepts_platform("discord-sophia", "discord-sophia") is True
    assert routing.accepts_platform("discord-sophia", "discord-sophia-backup") is False
    assert routing.accepts_platform("", "discord-sophia") is False


def test_platform_id_is_read_from_proactive_route():
    assert routing.platform_id_from_umo("discord-sophia:FriendMessage:user-42") == "discord-sophia"
