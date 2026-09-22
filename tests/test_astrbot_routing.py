from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from companion_gateway.affect import LABEL_DELTAS

_PATH = Path(__file__).parents[1] / "integrations" / "astrbot_companion_gateway" / "routing.py"
_SPEC = importlib.util.spec_from_file_location("astrbot_gateway_routing", _PATH)
assert _SPEC and _SPEC.loader
routing = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(routing)
_MAIN_PATH = _PATH.with_name("main.py")


def test_platform_selection_is_exact_and_disabled_when_empty():
    assert routing.accepts_platform("discord-sophia", "discord-sophia") is True
    assert routing.accepts_platform("discord-sophia", "discord-sophia-backup") is False
    assert routing.accepts_platform("", "discord-sophia") is False


def test_platform_id_is_read_from_proactive_route():
    assert routing.platform_id_from_umo("discord-sophia:FriendMessage:user-42") == "discord-sophia"


def test_affect_tool_exposes_one_fixed_label_parameter():
    tree = ast.parse(_MAIN_PATH.read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "record_affect_event"
    )
    assert [argument.arg for argument in function.args.args] == ["self", "event", "label"]
    assert function.args.defaults == []
    description = ast.get_docstring(function) or ""
    assert all(label in description for label in LABEL_DELTAS)
