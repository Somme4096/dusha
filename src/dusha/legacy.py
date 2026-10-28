from __future__ import annotations

import copy
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .homes import DOCS
from .resources import loads_strict

# Old layouts that still load during their deprecation. Remove each entry in the version it names.

# Prompts schema 3 to 4, removed in 2.0.21. A None target drops the key.
_PROMPTS_V3: dict[tuple[str, ...], tuple[str, ...] | None] = {
    ("affect_presentation",): ("affect", "presentation"),
    ("affect",): ("affect", "block"),
    ("decision_instruction",): ("affect", "decision_instruction"),
    ("proactive_framing",): ("proactive", "framing"),
    ("proactive_generation_instruction",): ("proactive", "generation_instruction"),
    ("evergreen", "open_delimiter"): ("evergreen", "open_delimiter"),
    ("evergreen", "close_delimiter"): ("evergreen", "close_delimiter"),
    ("context_blocks", "memo_open_delimiter"): ("memo", "open_delimiter"),
    ("context_blocks", "memo_close_delimiter"): ("memo", "close_delimiter"),
    ("context_blocks", "plugin_open_delimiter"): None,
    ("context_blocks", "plugin_close_delimiter"): None,
    ("companion_state", "memory_instruction"): ("memory", "instruction"),
}
_PROMPTS_V3_EMPTIED = ("evergreen", "context_blocks")


def upgrade_prompts(overlay: Any) -> tuple[Any, list[str]]:
    if not isinstance(overlay, dict):
        return overlay, []
    upgraded = copy.deepcopy(overlay)
    found: list[str] = []
    for old, new in _PROMPTS_V3.items():
        parent = upgraded
        for key in old[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        if not isinstance(parent, dict) or old[-1] not in parent:
            continue
        value = parent.pop(old[-1])
        found.append(".".join(old))
        if new is not None:
            plugins = upgraded.setdefault("plugins", {})
            if isinstance(plugins, dict) and isinstance(plugins.setdefault(new[0], {}), dict):
                plugins[new[0]].setdefault(new[1], value)
    for key in _PROMPTS_V3_EMPTIED:
        if upgraded.get(key) == {}:
            del upgraded[key]
    return upgraded, found


def _rewrite(path: Path, upgraded: dict[str, Any]) -> Path:
    backup = path.with_name(f"{path.name}.bak")
    shutil.copy2(path, backup)
    temp = path.with_name(f".{path.name}.tmp")
    temp.unlink(missing_ok=True)
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as file:
        file.write(json.dumps(upgraded, indent=2, ensure_ascii=False) + "\n")
    shutil.copymode(path, temp)
    os.replace(temp, path)
    return backup


def upgrade_prompts_file(path: Path) -> Path | None:
    upgraded, found = upgrade_prompts(loads_strict(path.read_text(encoding="utf-8"), source=str(path)))
    return _rewrite(path, upgraded) if found else None


# proxy.upstreams to top-level providers, removed in 2.0.21.
def upgrade_config(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    proxy = raw.get("proxy")
    if not isinstance(proxy, dict) or "upstreams" not in proxy:
        return raw, []
    upgraded = {**raw, "proxy": {key: value for key, value in proxy.items() if key != "upstreams"}}
    upstreams, providers = proxy["upstreams"], raw.get("providers") or {}
    if isinstance(upstreams, dict) and isinstance(providers, dict):
        upgraded["providers"] = {**upstreams, **providers}
    elif upstreams:
        upgraded["providers"] = upstreams
    return upgraded, ["proxy.upstreams"]


def warn_config(logger: logging.Logger, path: Path, found: list[str]) -> None:
    logger.warning(
        "%s uses %s, now top-level providers. It still loads until 2.0.21. "
        "Run dusha init to upgrade it, see %s",
        path, found, f"{DOCS}/blob/main/docs/guide.md#migrating",
    )


def upgrade_config_file(path: Path) -> Path | None:
    upgraded, found = upgrade_config(loads_strict(path.read_text(encoding="utf-8"), source=str(path)))
    return _rewrite(path, upgraded) if found else None
