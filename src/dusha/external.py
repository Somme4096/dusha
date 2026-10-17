from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PLUGIN_NAME, AppConfig

REQUIRED_FILES = ("pyproject.toml",)
PLUGINS_DIR_ENV = "DUSHA_PLUGINS_DIR"


class PluginError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedPlugin:
    name: str
    inject: tuple[str, ...]
    apply: Callable[..., None]


def resolve_plugins_dir(config: AppConfig) -> Path:
    env = os.getenv(PLUGINS_DIR_ENV, "")
    if env:
        return Path(env).expanduser()
    if config.plugins_dir:
        return Path(config.plugins_dir).expanduser()
    return (config.home or Path.cwd()) / "plugins"


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _suite(name: str, plugins_dir: Path) -> Path:
    if not PLUGIN_NAME.fullmatch(name):
        raise PluginError(f"plugin name must be a plain directory name, got {name!r}")
    root = plugins_dir.expanduser().resolve()
    suite = (root / name).resolve()
    if not _inside(suite, root) or not suite.is_dir():
        raise PluginError(f"plugin not found: {root / name}")
    for item in REQUIRED_FILES:
        path = suite / item
        if not path.is_file() or not _inside(path.resolve(), suite):
            raise PluginError(f"invalid plugin: missing or escaping {item}")
    return suite


def _manifest(suite: Path) -> tuple[str, tuple[str, ...]]:
    try:
        project = tomllib.loads((suite / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PluginError(f"invalid plugin manifest: {error}") from error
    section = project.get("tool", {}).get("dusha")
    if not isinstance(section, dict):
        raise PluginError("invalid plugin manifest: missing [tool.dusha]")
    unknown = sorted(set(section) - {"entry", "inject"})
    if unknown:
        raise PluginError(f"invalid plugin manifest: unknown field(s) under [tool.dusha]: {unknown}")
    entry = section.get("entry")
    if not isinstance(entry, str) or not all(part.isidentifier() for part in entry.split(".")):
        raise PluginError("invalid plugin manifest: tool.dusha.entry must be a module name")
    inject = section.get("inject", [])
    if not isinstance(inject, list) or not all(isinstance(item, str) and item for item in inject):
        raise PluginError("invalid plugin manifest: tool.dusha.inject must be an array of service names")
    return entry, tuple(inject)


def _module(name: str, entry: str, suite: Path) -> Any:
    top, _, rest = entry.partition(".")
    unique = f"dusha_plugin_{name}"
    for key in tuple(sys.modules):
        if key == unique or key.startswith(f"{unique}."):
            del sys.modules[key]
    for path in (suite, suite / "src"):
        text = str(path)
        if path.is_dir() and text not in sys.path:
            sys.path.insert(0, text)
    target = suite / top
    try:
        if target.is_dir():
            spec = importlib.util.spec_from_file_location(
                unique, target / "__init__.py", submodule_search_locations=[str(target)]
            )
        else:
            spec = importlib.util.spec_from_file_location(unique, suite / f"{top}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique] = module
        spec.loader.exec_module(module)
        if rest:
            return importlib.import_module(f"{unique}.{rest}")
        return module
    except Exception as error:
        for key in tuple(sys.modules):
            if key == unique or key.startswith(f"{unique}."):
                del sys.modules[key]
        raise PluginError(f"cannot import plugin entry {entry!r}: {error}") from error


def load(name: str, plugins_dir: Path) -> LoadedPlugin:
    suite = _suite(name, plugins_dir)
    entry, inject = _manifest(suite)
    module = _module(name, entry, suite)
    apply = getattr(module, "apply", None)
    if not callable(apply):
        raise PluginError(f"plugin entry {entry!r} does not define apply(ctx, config)")
    return LoadedPlugin(name, inject, apply)
