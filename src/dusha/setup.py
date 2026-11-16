from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import legacy
from .client import settings
from .config import ensure_server_config, load_config

CREATED = "created"
ADDED_SERVER = "added_server"
ADDED_PROXY = "added_proxy"
UPGRADED_CONFIG = "upgraded_config"
UPGRADED_PROMPTS = "upgraded_prompts"
NEEDS_PROXY = "needs_proxy"


@dataclass(frozen=True, slots=True)
class Change:
    kind: str
    path: Path
    backup: Path | None = None


def _server(home: Path, path: Path) -> list[Change]:
    existed = path.exists()
    if not ensure_server_config(home):
        return []
    return [Change(CREATED if not existed else ADDED_SERVER, path)]


def _proxy(path: Path, *, url: str, token: str, interactive: bool) -> list[Change]:
    saved = settings.saved_server(path)
    if url:
        if saved == (url, token):
            return []
        return [Change(ADDED_PROXY, settings.save_server(path, url, token))]
    if saved is not None:
        if not token or saved[1] == token:
            return []
        return [Change(ADDED_PROXY, settings.save_server(path, saved[0], token))]
    if interactive:
        return [Change(NEEDS_PROXY, path)]
    local = settings.local_server(path)
    if local is None:
        return [Change(NEEDS_PROXY, path)]
    return [Change(ADDED_PROXY, settings.save_server(path, local.url, token))]


def upgrade_files(path: Path) -> list[Change]:
    changes: list[Change] = []
    if backup := legacy.upgrade_config_file(path):
        changes.append(Change(UPGRADED_CONFIG, path, backup))
    prompts = load_config(path).prompts.path if settings.local_server(path) is not None else ""
    if prompts and (backup := legacy.upgrade_prompts_file(Path(prompts))):
        changes.append(Change(UPGRADED_PROMPTS, Path(prompts), backup))
    return changes


def ensure_config(
    home: Path,
    path: Path,
    *,
    server: bool = True,
    proxy: bool = True,
    upgrade: bool = True,
    url: str = "",
    token: str = "",
    interactive: bool = False,
) -> list[Change]:
    changes: list[Change] = []
    if server:
        changes += _server(home, path)
    if proxy:
        changes += _proxy(path, url=url, token=token, interactive=interactive)
    if upgrade and path.exists() and not any(change.kind == NEEDS_PROXY for change in changes):
        changes += upgrade_files(path)
    return changes
