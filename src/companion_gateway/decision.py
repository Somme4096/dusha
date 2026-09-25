"""Companion decision plugin contract, loading, and invocation.

A decision plugin is trusted Python loaded from a file under the configured
mods directory. It exports ``decide(request, options)`` and returns one emotion
dimension name (or None). The core validates the result against the resolved
emotion dimensions before it mutates state.

The fixed contract:

- ``DecisionRequest(message, emotions, state, instruction)`` carries the
  incoming message text, the resolved emotion dimension definitions, the current
  emotion snapshot, and the configured instruction from ``prompts.json``.
- ``DecisionResult(emotion)`` names exactly one dimension to increase.
- ``decide(request, options)`` returns a ``DecisionResult`` or ``None``.

Plugins are trusted code and are not sandboxed. The loader never raises into the
message path: a missing, broken, or malformed plugin logs a failure and leaves
decision-driven adjustment disabled so message ingest keeps working.
"""

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
# Both the namespace package and every loaded plugin live under this runtime
# package so a plugin's ``__module__`` resolves for dataclass, ``inspect``, and
# ``pickle`` while keeping plugin names out of the real import namespace.
_MODS_PACKAGE = "companion_gateway_mods"


@dataclass(frozen=True)
class DecisionRequest:
    """Inputs handed to a decision plugin for one incoming message."""

    message: str
    emotions: dict[str, dict[str, Any]]
    state: dict[str, Any]
    instruction: str


@dataclass(frozen=True)
class DecisionResult:
    """The single emotion dimension a plugin selects for adjustment."""

    emotion: str


class DecisionPluginError(RuntimeError):
    """Raised when a decision plugin cannot be resolved or is malformed."""


def resolve_mods_dir(config: Any) -> Path:
    """Return the effective mods directory for a config.

    ``COMPANION_GATEWAY_MODS_DIR`` is authoritative when set, which makes tests
    and operators able to point at an isolated mods tree. Otherwise an explicit
    ``decision.mods_dir`` wins. The default is ``<default config dir>/mods``.
    A relative explicit ``mods_dir`` was already resolved against the config
    file location when the config was loaded.
    """
    env = os.getenv("COMPANION_GATEWAY_MODS_DIR", "")
    if env:
        return Path(env).expanduser()
    decision = getattr(config, "decision", None)
    configured = str(getattr(decision, "mods_dir", "") or "")
    if configured:
        return Path(configured).expanduser()
    return default_config_dir() / "mods"


def _module_key(module_name: str, path: Path) -> str:
    """Build a deterministic, path-derived module identifier.

    Two same-named plugins from different directories get different keys, so
    neither can stomp the other's module identity or ``sys.modules`` entry.
    """
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    return f"{_MODS_PACKAGE}.{module_name}_{digest}"


def _ensure_mods_package(mods_dir: Path) -> None:
    """Register the plugin namespace package before a plugin is executed.

    A plugin may define dataclasses with ``from __future__ import annotations``;
    resolving those annotations requires ``sys.modules[cls.__module__]`` to
    exist. The namespace package also gives ``inspect`` a parent to walk.
    Multiple mods directories extend the same package's search path instead of
    replacing it, so no previously loaded plugin loses its identity.
    """
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
    """Load ``<module_name>.py`` from ``mods_dir`` only.

    The module name must be a plain identifier, so a configured value cannot
    traverse to another directory. The plugin is registered in ``sys.modules``
    before its code runs so standard library introspection keeps working; a
    failed execution removes that registration again and restores any prior
    module that occupied the same key.
    """
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
    """Load and validate the configured decision plugin.

    Returns the plugin's ``decide`` callable, or None when no module is
    configured or the plugin fails to load. Loading never raises: a broken
    plugin logs an error and leaves decision-driven adjustment disabled.
    """
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
    """Invoke the configured decision plugin with copied inputs.

    The provider hands the plugin deep copies of the emotion definitions and the
    state snapshot so a plugin cannot mutate engine-owned objects. A missing
    plugin, a raised exception, a None result, a malformed result, or an unknown
    dimension all yield None: no decision-driven adjustment.
    """

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
        """Return one allowed emotion dimension, or None."""
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
            # ``None`` is the documented abstention, not a malformed result.
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
