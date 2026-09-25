from __future__ import annotations

import copy
import hashlib
import importlib.machinery
import importlib.util
import logging
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .config import default_config_dir

logger = logging.getLogger("companion_gateway")

_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODS_PACKAGE = "companion_gateway_mods"


@dataclass(frozen=True)
class DecisionRequest:

    message: str
    emotions: dict[str, dict[str, Any]]
    state: dict[str, Any]
    instruction: str


@dataclass(frozen=True)
class DecisionResult:

    emotion: str


class DecisionPluginError(RuntimeError):
    pass


def resolve_mods_dir(config: Any) -> Path:
    env = os.getenv("COMPANION_GATEWAY_MODS_DIR", "")
    if env:
        return Path(env).expanduser()
    decision = getattr(config, "decision", None)
    configured = str(getattr(decision, "mods_dir", "") or "")
    if configured:
        return Path(configured).expanduser()
    return default_config_dir() / "mods"


def _module_key(module_name: str, path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    return f"{_MODS_PACKAGE}.{module_name}_{digest}"


def _ensure_mods_package(mods_dir: Path) -> None:
    package = sys.modules.get(_MODS_PACKAGE)
    if package is None:
        package = ModuleType(_MODS_PACKAGE)
        package.__spec__ = importlib.machinery.ModuleSpec(
            _MODS_PACKAGE, loader=None, is_package=True
        )
        package.__spec__.submodule_search_locations = [str(mods_dir)]
        package.__path__ = [str(mods_dir)]
        sys.modules[_MODS_PACKAGE] = package
        return
    paths = getattr(package, "__path__", None)
    if paths is None:
        paths = []
        package.__path__ = paths
    if str(mods_dir) not in paths:
        paths.insert(0, str(mods_dir))
        spec = getattr(package, "__spec__", None)
        if spec is not None and spec.submodule_search_locations is not None:
            spec.submodule_search_locations = list(paths)


def _load_module(module_name: str, mods_dir: Path) -> ModuleType:
    if not _MODULE_NAME.fullmatch(module_name):
        raise DecisionPluginError(
            f"decision.module must be a plain module name, got {module_name!r}"
        )
    path = mods_dir / f"{module_name}.py"
    if not path.is_file():
        raise DecisionPluginError(f"decision plugin not found: {path}")
    resolved = path.resolve()
    key = _module_key(module_name, resolved)
    existing = sys.modules.get(key)
    if existing is not None and getattr(existing, "__file__", None) == str(resolved):
        return existing
    _ensure_mods_package(mods_dir)
    spec = importlib.util.spec_from_file_location(key, resolved)
    if spec is None or spec.loader is None:
        raise DecisionPluginError(f"cannot load decision plugin: {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(key)
    sys.modules[key] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is not None:
            sys.modules[key] = previous
        else:
            sys.modules.pop(key, None)
        raise
    return module


def load_decider(config: Any) -> Callable[..., Any] | None:
    decision = getattr(config, "decision", None)
    module_name = str(getattr(decision, "module", "") or "").strip()
    if not module_name:
        logger.warning(
            "no decision module configured. Set decision.module to enable "
            "decision-driven emotional adjustments. No fallback will be used"
        )
        return None
    try:
        module = _load_module(module_name, resolve_mods_dir(config))
        decide = getattr(module, "decide", None)
        if not callable(decide):
            raise DecisionPluginError(
                f"decision plugin {module_name!r} does not export a callable decide()"
            )
        return decide
    except Exception:
        logger.exception(
            "failed to load decision plugin %r. Decision-driven adjustment disabled", module_name
        )
        return None


class DecisionProvider:

    def __init__(self, config: Any):
        self.config = config
        self.decide = load_decider(config)
        decision = getattr(config, "decision", None)
        self.increment = float(getattr(decision, "increment", 0.1))
        self.options = dict(getattr(decision, "options", {}) or {})

    @property
    def enabled(self) -> bool:
        return self.decide is not None

    def evaluate(
        self,
        *,
        message: str,
        emotions: dict[str, dict[str, Any]],
        state: dict[str, Any],
        instruction: str,
    ) -> str | None:
        if self.decide is None:
            return None
        request = DecisionRequest(
            message=str(message),
            emotions=copy.deepcopy(emotions),
            state=copy.deepcopy(state),
            instruction=str(instruction),
        )
        try:
            result = self.decide(request, copy.deepcopy(self.options))
        except Exception:
            logger.exception("decision plugin failed. No decision-driven adjustment")
            return None
        if result is None:
            return None
        try:
            emotion = getattr(result, "emotion", None)
        except Exception:
            logger.exception(
                "decision plugin result could not be read. No decision-driven adjustment"
            )
            return None
        if not isinstance(emotion, str) or emotion not in emotions:
            logger.warning("decision plugin returned invalid emotion: %r", emotion)
            return None
        return emotion
