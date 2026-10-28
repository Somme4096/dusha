from __future__ import annotations

import json
import logging
import math
import os
import re
import types as _types
import typing
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import emotions as _emotions
from . import legacy
from . import providers as _providers
from .defaults import all_defaults
from .homes import CONFIG_FILE, locate
from .resources import loads_strict

_DEFAULTS = all_defaults()
_EMOTIONS = _emotions.default_emotions()
_PLUGINS = _DEFAULTS["plugins"]
_MEMORY = _PLUGINS["memory"]
_EMBEDDING = _DEFAULTS["embedding"]
_EVERGREEN = _PLUGINS["evergreen"]
_MEMO = _PLUGINS["memo"]
_AFFECT_KNOBS = _EMOTIONS["affect"]
_PROACTIVE = _PLUGINS["proactive"]
_PROMPTS = _DEFAULTS["prompts"]


IDENTITY_FILE = "identity.md"
CLIENT_SECTIONS = frozenset({"proxy"})
SHARED_SECTIONS = frozenset({"providers"})
PLUGIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _write_new(path: Path, raw: dict[str, Any]) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as file:
        file.write(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")


def ensure_server_config(home: Path) -> str:
    path = home / CONFIG_FILE
    defaults = {**all_defaults(), "data_dir": "data"}
    if not path.exists():
        home.mkdir(parents=True, exist_ok=True)
        _write_new(path, defaults)
        return f"created {path}"
    raw = _read_config_file(path)
    if set(raw) - CLIENT_SECTIONS - SHARED_SECTIONS:
        return ""
    temp = path.with_name(f".{path.name}.tmp")
    temp.unlink(missing_ok=True)
    _write_new(temp, {**defaults, **raw})
    os.replace(temp, path)
    return f"added the server keys to {path}"


_UNSET: Any = _emotions.UNSET
_EMOTIONS_FILE_FIELDS = {"emotions_path", "expected_emotion_version"}
_ENTRY_FIELDS = {"disabled"}


def _fill_unset(config_obj: Any, knobs: dict[str, Any], explicit_attr: str) -> None:
    explicit: set[str] = set()
    for name, default in knobs.items():
        value = getattr(config_obj, name)
        if value is _UNSET:
            setattr(config_obj, name, default)
        else:
            explicit.add(name)
    setattr(config_obj, explicit_attr, frozenset(explicit))


_BOUNDS: dict[str, dict[str, tuple[float | None, float | None]]] = {
    "embedding": {
        "dimensions": (1, None),
        "timeout_seconds": (0, None),
        "batch_size": (1, None),
        "failure_cooldown_seconds": (0, None),
    },
    "plugins.memory": {
        "recent_messages": (0, None),
        "search_hits": (0, None),
        "context_messages": (0, None),
    },
    "plugins.evergreen": {"max_items": (0, None), "max_chars": (0, None)},
    "plugins.memo": {
        "max_items": (0, 500),
        "max_chars": (0, None),
        "text_max_chars": (1, None),
        "reason_max_chars": (1, None),
    },
    "plugins.proactive": {
        "poll_interval_seconds": (1, None),
        "min_silence_minutes": (0, None),
        "cooldown_minutes": (0, None),
        "max_per_day": (0, None),
        "max_unanswered": (0, None),
        "quiet_start_hour": (0, 23),
        "quiet_end_hour": (0, 23),
        "lease_seconds": (1, None),
        "retry_delay_minutes": (0, None),
    },
}
_EXCLUSIVE_ZERO = {"timeout_seconds"}


def _check_bounds(section: Any, name: str) -> None:
    for key, (low, high) in _BOUNDS[name].items():
        value = getattr(section, key)
        if value is None:
            continue
        below = low is not None and (value <= low if key in _EXCLUSIVE_ZERO else value < low)
        if below or (high is not None and value > high) or not math.isfinite(value):
            edge = "above" if key in _EXCLUSIVE_ZERO else "at least"
            limit = f"{edge} {low}" + (f" and at most {high}" if high is not None else "")
            raise ValueError(f"{name}.{key} must be {limit}")


@dataclass(slots=True)
class EmbeddingConfig:
    base_url: str = _EMBEDDING["base_url"]
    api_key_env: str = _EMBEDDING["api_key_env"]
    model: str = _EMBEDDING["model"]
    dimensions: int | None = _EMBEDDING["dimensions"]
    timeout_seconds: float = _EMBEDDING["timeout_seconds"]
    batch_size: int = _EMBEDDING["batch_size"]
    failure_cooldown_seconds: int = _EMBEDDING["failure_cooldown_seconds"]

    def __post_init__(self) -> None:
        _check_bounds(self, "embedding")


@dataclass(slots=True)
class MemoryConfig:
    recent_messages: int = _MEMORY["recent_messages"]
    search_hits: int = _MEMORY["search_hits"]
    context_messages: int = _MEMORY["context_messages"]

    def __post_init__(self) -> None:
        _check_bounds(self, "plugins.memory")


@dataclass(slots=True)
class EvergreenConfig:
    max_items: int = _EVERGREEN["max_items"]
    max_chars: int = _EVERGREEN["max_chars"]

    def __post_init__(self) -> None:
        _check_bounds(self, "plugins.evergreen")


@dataclass(slots=True)
class MemoConfig:
    max_items: int = _MEMO["max_items"]
    max_chars: int = _MEMO["max_chars"]
    text_max_chars: int = _MEMO["text_max_chars"]
    reason_max_chars: int = _MEMO["reason_max_chars"]

    def __post_init__(self) -> None:
        _check_bounds(self, "plugins.memo")


@dataclass(slots=True)
class AffectConfig:
    emotions_path: str = ""
    expected_emotion_version: str = ""
    mood_follow_hours: float = _UNSET
    mood_return_hours: float = _UNSET
    dimensions: dict[str, dict[str, float]] = field(default_factory=dict)
    silence: dict[str, dict[str, float]] = field(default_factory=dict)
    increment: float | None = None
    explicit_knobs: frozenset[str] = field(default=frozenset(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _fill_unset(self, _AFFECT_KNOBS, "explicit_knobs")
        value = self.increment
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("plugins.affect.increment must be null or a finite positive number")
            if not math.isfinite(float(value)) or value <= 0:
                raise ValueError("plugins.affect.increment must be null or a finite positive number")
            self.increment = float(value)


@dataclass(slots=True)
class ExternalPluginConfig:
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProactiveConfig:
    poll_interval_seconds: int = _PROACTIVE["poll_interval_seconds"]
    min_silence_minutes: int = _PROACTIVE["min_silence_minutes"]
    thresholds: dict[str, float] = field(default_factory=dict)
    cooldown_minutes: int = _PROACTIVE["cooldown_minutes"]
    max_per_day: int = _PROACTIVE["max_per_day"]
    max_unanswered: int = _PROACTIVE["max_unanswered"]
    quiet_start_hour: int = _PROACTIVE["quiet_start_hour"]
    quiet_end_hour: int = _PROACTIVE["quiet_end_hour"]
    lease_seconds: int = _PROACTIVE["lease_seconds"]
    retry_delay_minutes: int = _PROACTIVE["retry_delay_minutes"]

    def __post_init__(self) -> None:
        _check_bounds(self, "plugins.proactive")


@dataclass(slots=True)
class IdentityConfig:
    path: str = ""


BUILTIN_CONFIGS: dict[str, type[Any]] = {
    "identity": IdentityConfig,
    "memory": MemoryConfig,
    "evergreen": EvergreenConfig,
    "memo": MemoConfig,
    "affect": AffectConfig,
    "proactive": ProactiveConfig,
}


@dataclass(slots=True)
class PluginEntry:
    config: Any
    disabled: bool = False


def _plugin_entries(plugins: dict[str, Any]) -> dict[str, PluginEntry]:
    entries: dict[str, PluginEntry] = {}
    for name, value in plugins.items():
        if not PLUGIN_NAME.fullmatch(str(name)):
            raise ValueError(f"plugin name must be a plain directory name, got {name!r}")
        if value is False:
            default = BUILTIN_CONFIGS.get(name, ExternalPluginConfig)
            value = PluginEntry(default(), disabled=True)
        elif not isinstance(value, PluginEntry):
            value = PluginEntry(value)
        expected = BUILTIN_CONFIGS.get(name, ExternalPluginConfig)
        if not isinstance(value.config, expected):
            raise ValueError(f"plugins.{name} must be a {expected.__name__}")
        entries[name] = value
    missing = {name: PluginEntry(cls()) for name, cls in BUILTIN_CONFIGS.items() if name not in entries}
    return {**missing, **entries}


@dataclass(slots=True)
class PromptsConfig:
    path: str = _PROMPTS["path"]


@dataclass(slots=True)
class AppConfig:
    home: Path | None = field(default=None, repr=False, compare=False)
    data_dir: Path = Path(_DEFAULTS["data_dir"])
    host: str = _DEFAULTS["host"]
    port: int = _DEFAULTS["port"]
    timezone: str = _DEFAULTS["timezone"]
    api_token: str = _DEFAULTS["api_token"]
    plugins_dir: str = _DEFAULTS["plugins_dir"]
    prompts: PromptsConfig = field(default_factory=lambda: PromptsConfig())
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    plugins: dict[str, Any] = field(default_factory=dict)
    providers: dict[str, _providers.Provider] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.plugins = _plugin_entries(self.plugins)
        if isinstance(self.port, bool) or not 0 <= self.port <= 65535:
            raise ValueError("port must be at least 0 and at most 65535")
        try:
            zoneinfo.ZoneInfo(self.timezone)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"timezone is not a known IANA zone: {self.timezone!r}") from error

    @property
    def database_path(self) -> Path:
        return self.data_dir / "state.sqlite3"

    def plugin(self, name: str) -> Any:
        return self.plugins[name].config




def _type_ok(value: Any, annotation: Any) -> bool:
    if annotation is type(None):
        return value is None
    if annotation is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if annotation is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if annotation is str:
        return isinstance(value, str)
    if annotation is bool:
        return isinstance(value, bool)
    if annotation is Path:
        return isinstance(value, (str, Path))
    return True


def _validate_value(name: str, value: Any, annotation: Any) -> None:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, _types.UnionType):
        for arg in typing.get_args(annotation):
            if _type_ok(value, arg):
                return
        raise ValueError(f"{name} must match one of {list(typing.get_args(annotation))}")
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")
        return
    if origin is list:
        if not isinstance(value, list):
            raise ValueError(f"{name} must be an array")
        return
    if not _type_ok(value, annotation):
        raise ValueError(f"{name} must be {getattr(annotation, '__name__', annotation)}")


def _strict_section(cls: type[Any], raw: Any, name: str) -> Any:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be an object")
    hints = typing.get_type_hints(cls)
    settable = {field_name for field_name, spec in cls.__dataclass_fields__.items() if spec.init}
    unknown = sorted(set(raw) - settable)
    if unknown:
        raise ValueError(f"unknown field(s) under {name}: {unknown}")
    for key, value in raw.items():
        _validate_value(f"{name}.{key}", value, hints[key])
    return cls(**raw)


def _read_config_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return loads_strict(text, source=str(path))


def _path_section(cls: type[Any], raw: Any, name: str, source_dir: Path) -> Any:
    section = _strict_section(cls, raw, name)
    if section.path and not Path(section.path).is_absolute():
        section.path = str((source_dir / Path(section.path).expanduser()).resolve())
    return section


def _resolve_path(value: str, source_dir: Path) -> str:
    if not value:
        return ""
    resolved = Path(value).expanduser()
    if not resolved.is_absolute():
        resolved = source_dir / resolved
    return str(resolved.resolve())


def _identity_section(raw: dict[str, Any], label: str, source_dir: Path) -> IdentityConfig:
    identity = _strict_section(IdentityConfig, raw, label)
    if "path" in raw and not identity.path:
        raise ValueError(
            f"{label}.path is empty. Remove the key to use {IDENTITY_FILE}, "
            f"or set {label}.disabled to true"
        )
    if identity.path:
        identity.path = _resolve_path(identity.path, source_dir)
    elif (source_dir / IDENTITY_FILE).is_file():
        identity.path = str((source_dir / IDENTITY_FILE).resolve())
    return identity


def _affect_section(raw: dict[str, Any], label: str, source_dir: Path) -> AffectConfig:
    misplaced = sorted(set(raw) & _EMOTIONS_FILE_FIELDS)
    if misplaced:
        raise ValueError(
            f"unknown field(s) under {label}: {misplaced}. "
            f"Use {label}.emotions.path and {label}.emotions.expected_version"
        )
    fields = dict(raw)
    emotions_raw = fields.pop("emotions", None) or {}
    if not isinstance(emotions_raw, dict):
        raise ValueError(f"{label}.emotions must be an object")
    unknown = sorted(set(emotions_raw) - {"path", "expected_version"})
    if unknown:
        raise ValueError(f"unknown field(s) under {label}.emotions: {unknown}")
    affect = _strict_section(AffectConfig, fields, label)
    path_value = emotions_raw.get("path", "")
    if not isinstance(path_value, str):
        raise ValueError(f"{label}.emotions.path must be a string")
    affect.emotions_path = _resolve_path(path_value, source_dir)
    version = emotions_raw.get("expected_version", "")
    if not isinstance(version, str):
        raise ValueError(f"{label}.emotions.expected_version must be a string")
    affect.expected_emotion_version = version
    return affect


def _plugin_section(name: str, raw: Any, source_dir: Path) -> PluginEntry:
    label = f"plugins.{name}"
    if not PLUGIN_NAME.fullmatch(name):
        raise ValueError(f"plugin name must be a plain directory name, got {name!r}")
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    fields = dict(raw)
    disabled = fields.pop("disabled", False)
    if not isinstance(disabled, bool):
        raise ValueError(f"{label}.disabled must be bool")
    if name == "identity":
        config: Any = _identity_section(fields, label, source_dir) if not disabled else IdentityConfig()
    elif name == "affect":
        config = _affect_section(fields, label, source_dir)
    else:
        config = _strict_section(BUILTIN_CONFIGS.get(name, ExternalPluginConfig), fields, label)
    return PluginEntry(config, disabled)


def _app_config_from_raw(raw: dict[str, Any], source_dir: Path) -> AppConfig:
    allowed = set(typing.get_type_hints(AppConfig)) - {"home"} | CLIENT_SECTIONS
    moved = sorted(set(raw) & {"api_openai", "upstream"})
    if moved:
        raise ValueError(f"{moved} moved to providers, see https://github.com/Somme4096/dusha")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unknown top-level field(s): {unknown}")

    plugins_raw = raw.get("plugins") or {}
    if not isinstance(plugins_raw, dict):
        raise ValueError("plugins must be an object")
    missing = [name for name in BUILTIN_CONFIGS if name not in plugins_raw]
    plugins = {name: _plugin_section(name, {}, source_dir) for name in missing}
    for name, section in plugins_raw.items():
        plugins[str(name)] = _plugin_section(str(name), section, source_dir)
    prompts = _path_section(PromptsConfig, raw.get("prompts"), "prompts", source_dir)

    hints = typing.get_type_hints(AppConfig)
    kwargs: dict[str, Any] = {}
    for key in ("host", "port", "timezone", "api_token", "plugins_dir"):
        if key in raw:
            _validate_value(key, raw[key], hints[key])
            kwargs[key] = raw[key]
    if kwargs.get("plugins_dir"):
        kwargs["plugins_dir"] = _resolve_path(kwargs["plugins_dir"], source_dir)
    if "data_dir" in raw:
        _validate_value("data_dir", raw["data_dir"], Path)
        data_dir = Path(raw["data_dir"]).expanduser()
        if not data_dir.is_absolute():
            data_dir = source_dir / data_dir
        kwargs["data_dir"] = data_dir
    else:
        kwargs["data_dir"] = source_dir / Path(_DEFAULTS["data_dir"])

    return AppConfig(
        **kwargs,
        prompts=prompts,
        embedding=_strict_section(EmbeddingConfig, raw.get("embedding"), "embedding"),
        plugins=plugins,
        providers=_providers.parse(raw.get("providers"), environ=False),
    )


def load_config(
    path: str | Path | None = None,
    *,
    home: str | Path | None = None,
    companion: str | None = None,
) -> AppConfig:
    config_path = locate(path, home=home, companion=companion)
    if config_path is None:
        return AppConfig()
    raw, found = legacy.upgrade_config(_read_config_file(config_path))
    if found:
        legacy.warn_config(logging.getLogger("dusha"), config_path, found)
    cfg = _app_config_from_raw(raw, config_path.parent)
    cfg.home = config_path.parent
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _validate_packaged_defaults() -> None:
    hints = typing.get_type_hints(AppConfig)
    for key in ("data_dir", "host", "port", "timezone", "api_token", "plugins_dir"):
        _validate_value(f"packaged defaults {key}", _DEFAULTS[key], hints[key])
    _strict_section(EmbeddingConfig, _DEFAULTS["embedding"], "packaged defaults embedding")
    _strict_section(PromptsConfig, _DEFAULTS["prompts"], "packaged defaults prompts")
    for name, raw in _DEFAULTS["plugins"].items():
        _plugin_section(name, raw, Path.cwd())


_validate_packaged_defaults()


def __getattr__(name: str) -> Any:
    return legacy.alias(__name__, name)
