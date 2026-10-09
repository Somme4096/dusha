from __future__ import annotations

import json
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
from .defaults import all_defaults
from .resources import loads_strict

_DEFAULTS = all_defaults()
_EMOTIONS = _emotions.default_emotions()
_UPSTREAM = _DEFAULTS["upstream"]
_API_OPENAI = _DEFAULTS["api_openai"]
_MEMORY = _DEFAULTS["memory"]
_EMBEDDING = _DEFAULTS["embedding"]
_EVERGREEN = _DEFAULTS["evergreen"]
_MEMO = _DEFAULTS["memo"]
_DECISION = _DEFAULTS["decision"]
_STORAGE = _DEFAULTS["storage"]
_MEMORY_PLUGIN = _DEFAULTS["memory_plugin"]
_AFFECT_KNOBS = _EMOTIONS["affect"]
_PROACTIVE = _DEFAULTS["proactive"]
_IDENTITY_PROMPT = _DEFAULTS["identity_prompt"]
_PROMPTS = _DEFAULTS["prompts"]


CONFIG_FILE = "config.json"
HOME_ENV = "DUSHA_HOME"
COMPANION_ENV = "DUSHA_COMPANION"
_COMPANION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def config_root() -> Path:
    base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "dusha"


def companion_home(name: str) -> Path:
    if not _COMPANION_NAME.fullmatch(name):
        raise ValueError(f"companion name must be letters, digits, dot, dash, or underscore, got {name!r}")
    return config_root() / name


def create_default_config(home: Path) -> Path:
    raw = {"emotions": {"path": "", "expected_version": ""}, **all_defaults(), "data_dir": "data"}
    home.mkdir(parents=True, exist_ok=True)
    path = home / CONFIG_FILE
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def list_companions() -> list[str]:
    root = config_root()
    if not root.is_dir():
        return []
    return sorted(item.name for item in root.iterdir() if (item / CONFIG_FILE).is_file())


_UNSET: Any = _emotions.UNSET
_EMOTIONS_FILE_FIELDS = {"emotions_path", "expected_emotion_version"}


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
    "upstream": {"timeout_seconds": (0, None)},
    "embedding": {
        "dimensions": (1, None),
        "timeout_seconds": (0, None),
        "batch_size": (1, None),
        "backfill_interval_seconds": (1, None),
        "failure_cooldown_seconds": (0, None),
    },
    "memory": {
        "recent_messages": (0, None),
        "search_hits": (0, None),
        "context_messages": (0, None),
        "injection_max_chars": (1, None),
        "plugin_context_max_chars": (0, None),
    },
    "evergreen": {"max_items": (0, None), "max_chars": (0, None)},
    "memo": {
        "max_items": (0, 500),
        "max_chars": (0, None),
        "text_max_chars": (1, None),
        "reason_max_chars": (1, None),
    },
    "proactive": {
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


def _check_env_names(value: Any, name: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{name} must be an array of environment variable names")


@dataclass(slots=True)
class UpstreamConfig:
    base_url: str = _UPSTREAM["base_url"]
    api_key_env: str = _UPSTREAM["api_key_env"]
    timeout_seconds: float = _UPSTREAM["timeout_seconds"]
    allowed_hosts: list[str] = field(default_factory=lambda: list(_UPSTREAM["allowed_hosts"]))

    def __post_init__(self) -> None:
        _check_bounds(self, "upstream")
        hosts = self.allowed_hosts
        if not isinstance(hosts, list) or not all(isinstance(item, str) and item for item in hosts):
            raise ValueError("upstream.allowed_hosts must be an array of host patterns")


@dataclass(slots=True)
class ApiOpenAIConfig:
    enabled: bool = _API_OPENAI["enabled"]


@dataclass(slots=True)
class EmbeddingConfig:
    base_url: str = _EMBEDDING["base_url"]
    api_key_env: str = _EMBEDDING["api_key_env"]
    model: str = _EMBEDDING["model"]
    dimensions: int | None = _EMBEDDING["dimensions"]
    timeout_seconds: float = _EMBEDDING["timeout_seconds"]
    batch_size: int = _EMBEDDING["batch_size"]
    backfill_interval_seconds: int = _EMBEDDING["backfill_interval_seconds"]
    failure_cooldown_seconds: int = _EMBEDDING["failure_cooldown_seconds"]

    def __post_init__(self) -> None:
        _check_bounds(self, "embedding")


@dataclass(slots=True)
class MemoryConfig:
    recent_messages: int = _MEMORY["recent_messages"]
    search_hits: int = _MEMORY["search_hits"]
    context_messages: int = _MEMORY["context_messages"]
    injection_max_chars: int = _MEMORY["injection_max_chars"]
    plugin_context_max_chars: int = _MEMORY["plugin_context_max_chars"]

    def __post_init__(self) -> None:
        _check_bounds(self, "memory")


@dataclass(slots=True)
class EvergreenConfig:
    enabled: bool = _EVERGREEN["enabled"]
    max_items: int = _EVERGREEN["max_items"]
    max_chars: int = _EVERGREEN["max_chars"]

    def __post_init__(self) -> None:
        _check_bounds(self, "evergreen")


@dataclass(slots=True)
class MemoConfig:
    max_items: int = _MEMO["max_items"]
    max_chars: int = _MEMO["max_chars"]
    text_max_chars: int = _MEMO["text_max_chars"]
    reason_max_chars: int = _MEMO["reason_max_chars"]

    def __post_init__(self) -> None:
        _check_bounds(self, "memo")


@dataclass(slots=True)
class StorageConfig:
    enabled: bool = _STORAGE["enabled"]


@dataclass(slots=True)
class AffectConfig:
    emotions_path: str = ""
    expected_emotion_version: str = ""
    mood_follow_hours: float = _UNSET
    mood_return_hours: float = _UNSET
    dimensions: dict[str, dict[str, float]] = field(default_factory=dict)
    silence: dict[str, dict[str, float]] = field(default_factory=dict)
    explicit_knobs: frozenset[str] = field(default=frozenset(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _fill_unset(self, _AFFECT_KNOBS, "explicit_knobs")


@dataclass(slots=True)
class DecisionConfig:

    module: str = _DECISION["module"]
    options: dict[str, Any] = field(default_factory=lambda: dict(_DECISION["options"]))
    increment: float | None = _DECISION["increment"]
    mods_dir: str = _DECISION["mods_dir"]
    timeout_seconds: float = _DECISION["timeout_seconds"]
    env_passthrough: list[str] = field(default_factory=lambda: list(_DECISION["env_passthrough"]))

    def __post_init__(self) -> None:
        _check_env_names(self.env_passthrough, "decision.env_passthrough")
        value = self.increment
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("decision.increment must be null or a finite positive number")
            if not math.isfinite(float(value)) or value <= 0:
                raise ValueError("decision.increment must be null or a finite positive number")
            self.increment = float(value)
        timeout = self.timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("decision.timeout_seconds must be a finite positive number")
        timeout_number = float(timeout)
        if not math.isfinite(timeout_number) or timeout_number <= 0:
            raise ValueError("decision.timeout_seconds must be a finite positive number")
        self.timeout_seconds = timeout_number


@dataclass(slots=True)
class MemoryPluginConfig:
    module: str = _MEMORY_PLUGIN["module"]
    options: dict[str, Any] = field(default_factory=lambda: dict(_MEMORY_PLUGIN["options"]))
    mods_dir: str = _MEMORY_PLUGIN["mods_dir"]
    timeout_seconds: float = _MEMORY_PLUGIN["timeout_seconds"]
    ingest_batch_size: int = _MEMORY_PLUGIN["ingest_batch_size"]
    ingest_backfill_interval_seconds: int = _MEMORY_PLUGIN["ingest_backfill_interval_seconds"]
    env_passthrough: list[str] = field(default_factory=lambda: list(_MEMORY_PLUGIN["env_passthrough"]))

    def __post_init__(self) -> None:
        _check_env_names(self.env_passthrough, "memory_plugin.env_passthrough")
        value = self.timeout_seconds
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("memory_plugin.timeout_seconds must be a finite positive number")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError("memory_plugin.timeout_seconds must be a finite positive number")
        self.timeout_seconds = number
        batch = self.ingest_batch_size
        if isinstance(batch, bool) or not isinstance(batch, int) or not 1 <= batch <= 500:
            raise ValueError("memory_plugin.ingest_batch_size must be an integer between 1 and 500")
        interval = self.ingest_backfill_interval_seconds
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
            raise ValueError("memory_plugin.ingest_backfill_interval_seconds must be a positive integer")


@dataclass(slots=True)
class ProactiveConfig:
    enabled: bool = _PROACTIVE["enabled"]
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
        _check_bounds(self, "proactive")


@dataclass(slots=True)
class IdentityPromptConfig:
    path: str = _IDENTITY_PROMPT["path"]


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
    api_token_env: str = _DEFAULTS["api_token_env"]
    api_openai: ApiOpenAIConfig = field(default_factory=ApiOpenAIConfig)
    identity_prompt: IdentityPromptConfig = field(
        default_factory=lambda: IdentityPromptConfig()
    )
    prompts: PromptsConfig = field(default_factory=lambda: PromptsConfig())
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    evergreen: EvergreenConfig = field(default_factory=EvergreenConfig)
    memo: MemoConfig = field(default_factory=MemoConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    affect: AffectConfig = field(default_factory=lambda: AffectConfig())
    proactive: ProactiveConfig = field(default_factory=lambda: ProactiveConfig())
    decision: DecisionConfig = field(default_factory=lambda: DecisionConfig())
    memory_plugin: MemoryPluginConfig = field(default_factory=lambda: MemoryPluginConfig())

    def __post_init__(self) -> None:
        if self.memory_plugin.module and not self.storage.enabled:
            raise ValueError("memory_plugin.module requires storage.enabled")
        if isinstance(self.port, bool) or not 0 <= self.port <= 65535:
            raise ValueError("port must be at least 0 and at most 65535")
        try:
            zoneinfo.ZoneInfo(self.timezone)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"timezone is not a known IANA zone: {self.timezone!r}") from error

    @property
    def database_path(self) -> Path:
        return self.data_dir / "state.sqlite3"




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


def _app_config_from_raw(raw: dict[str, Any], source_dir: Path) -> AppConfig:
    allowed = (set(typing.get_type_hints(AppConfig)) | {"emotions"}) - {"home"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unknown top-level field(s): {unknown}")

    affect_raw = raw.get("affect")
    misplaced = sorted(set(affect_raw) & _EMOTIONS_FILE_FIELDS) if isinstance(affect_raw, dict) else []
    if misplaced:
        raise ValueError(
            f"unknown field(s) under affect: {misplaced}. Use emotions.path and emotions.expected_version"
        )
    affect = _strict_section(AffectConfig, raw.get("affect"), "affect")
    identity_prompt = _path_section(
        IdentityPromptConfig, raw.get("identity_prompt"), "identity_prompt", source_dir
    )
    prompts = _path_section(PromptsConfig, raw.get("prompts"), "prompts", source_dir)
    decision = _strict_section(DecisionConfig, raw.get("decision"), "decision")
    if decision.mods_dir and not Path(decision.mods_dir).is_absolute():
        decision.mods_dir = str((source_dir / Path(decision.mods_dir).expanduser()).resolve())
    memory_plugin = _strict_section(
        MemoryPluginConfig, raw.get("memory_plugin"), "memory_plugin"
    )
    if memory_plugin.mods_dir and not Path(memory_plugin.mods_dir).is_absolute():
        memory_plugin.mods_dir = str((source_dir / Path(memory_plugin.mods_dir).expanduser()).resolve())
    emotions_raw = raw.get("emotions")
    if emotions_raw is not None:
        if not isinstance(emotions_raw, dict):
            raise ValueError("emotions must be an object")
        emo_unknown = sorted(set(emotions_raw) - {"path", "expected_version"})
        if emo_unknown:
            raise ValueError(f"unknown field(s) under emotions: {emo_unknown}")
        if "path" in emotions_raw:
            path_value = emotions_raw["path"]
            if not isinstance(path_value, str):
                raise ValueError("emotions.path must be a string")
            if path_value:
                resolved = Path(path_value).expanduser()
                if not resolved.is_absolute():
                    resolved = source_dir / resolved
                affect.emotions_path = str(resolved)
            else:
                affect.emotions_path = ""
        if "expected_version" in emotions_raw:
            version = emotions_raw["expected_version"]
            if not isinstance(version, str):
                raise ValueError("emotions.expected_version must be a string")
            affect.expected_emotion_version = version

    hints = typing.get_type_hints(AppConfig)
    kwargs: dict[str, Any] = {}
    for key in ("host", "port", "timezone", "api_token_env"):
        if key in raw:
            _validate_value(key, raw[key], hints[key])
            kwargs[key] = raw[key]
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
        identity_prompt=identity_prompt,
        prompts=prompts,
        upstream=_strict_section(UpstreamConfig, raw.get("upstream"), "upstream"),
        embedding=_strict_section(EmbeddingConfig, raw.get("embedding"), "embedding"),
        api_openai=_strict_section(ApiOpenAIConfig, raw.get("api_openai"), "api_openai"),
        memory=_strict_section(MemoryConfig, raw.get("memory"), "memory"),
        evergreen=_strict_section(EvergreenConfig, raw.get("evergreen"), "evergreen"),
        memo=_strict_section(MemoConfig, raw.get("memo"), "memo"),
        storage=_strict_section(StorageConfig, raw.get("storage"), "storage"),
        affect=affect,
        proactive=_strict_section(ProactiveConfig, raw.get("proactive"), "proactive"),
        decision=decision,
        memory_plugin=memory_plugin,
    )


def _config_file(explicit: str | Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(explicit))))
    if path.suffix.lower() != ".json":
        raise ValueError(f"unsupported configuration format: {path}. Use a JSON file")
    if not path.exists():
        raise FileNotFoundError(f"configuration file not found: {path}")
    return path


def resolve_home(home: str | Path | None = None, companion: str | None = None) -> Path | None:
    if home:
        return Path(os.path.expandvars(os.path.expanduser(str(home))))
    if companion:
        return companion_home(companion)
    if os.getenv(HOME_ENV):
        return Path(os.path.expandvars(os.path.expanduser(os.environ[HOME_ENV])))
    if os.getenv(COMPANION_ENV):
        return companion_home(os.environ[COMPANION_ENV])
    names = list_companions()
    if len(names) > 1:
        raise ValueError(f"several companions exist: {names}. Name one")
    return companion_home(names[0]) if names else None


def load_config(
    path: str | Path | None = None,
    *,
    home: str | Path | None = None,
    companion: str | None = None,
) -> AppConfig:
    if path is not None:
        config_path = _config_file(path)
    else:
        selected = resolve_home(home, companion)
        if selected is None:
            return AppConfig()
        config_path = _config_file(selected / CONFIG_FILE)
    cfg = _app_config_from_raw(_read_config_file(config_path), config_path.parent)
    cfg.home = config_path.parent
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _validate_packaged_defaults() -> None:
    hints = typing.get_type_hints(AppConfig)
    for key in ("data_dir", "host", "port", "timezone", "api_token_env"):
        _validate_value(f"packaged defaults {key}", _DEFAULTS[key], hints[key])
    _strict_section(UpstreamConfig, _DEFAULTS["upstream"], "packaged defaults upstream")
    _strict_section(EmbeddingConfig, _DEFAULTS["embedding"], "packaged defaults embedding")
    _strict_section(ApiOpenAIConfig, _DEFAULTS["api_openai"], "packaged defaults api_openai")
    _strict_section(MemoryConfig, _DEFAULTS["memory"], "packaged defaults memory")
    _strict_section(EvergreenConfig, _DEFAULTS["evergreen"], "packaged defaults evergreen")
    _strict_section(MemoConfig, _DEFAULTS["memo"], "packaged defaults memo")
    _strict_section(StorageConfig, _DEFAULTS["storage"], "packaged defaults storage")
    _strict_section(ProactiveConfig, _DEFAULTS["proactive"], "packaged defaults proactive")
    _strict_section(DecisionConfig, _DEFAULTS["decision"], "packaged defaults decision")
    _strict_section(MemoryPluginConfig, _DEFAULTS["memory_plugin"], "packaged defaults memory_plugin")
    _strict_section(
        IdentityPromptConfig, _DEFAULTS["identity_prompt"], "packaged defaults identity_prompt"
    )
    _strict_section(PromptsConfig, _DEFAULTS["prompts"], "packaged defaults prompts")


_validate_packaged_defaults()
