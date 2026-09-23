from __future__ import annotations

import copy
import json
import os
import re
import types as _types
import typing
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import emotions as _emotions
from .defaults import all_defaults
from .resources import loads_strict

_DEFAULTS = all_defaults()
_EMOTIONS = _emotions.default_emotions()
_UPSTREAM = _DEFAULTS["upstream"]
_MEMORY = _DEFAULTS["memory"]
_EMBEDDING = _MEMORY["embedding"]
_EVERGREEN = _DEFAULTS["evergreen"]
_AFFECT_KNOBS = _EMOTIONS["affect"]
_PROACTIVE_EMOTIONAL = _EMOTIONS["proactive"]
_PROACTIVE = _DEFAULTS["proactive"]

# Config fields for emotional knobs default to this sentinel. __post_init__
# fills unset fields from the packaged emotional defaults and records which
# fields the caller explicitly provided, so resolution never guesses whether a
# value came from the caller or from a default.
_UNSET: Any = _emotions.UNSET


@dataclass(slots=True)
class UpstreamConfig:
    base_url: str = _UPSTREAM["base_url"]
    api_key_env: str = _UPSTREAM["api_key_env"]
    timeout_seconds: float = _UPSTREAM["timeout_seconds"]


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


@dataclass(slots=True)
class MemoryConfig:
    recent_messages: int = _MEMORY["recent_messages"]
    search_hits: int = _MEMORY["search_hits"]
    context_messages: int = _MEMORY["context_messages"]
    injection_max_chars: int = _MEMORY["injection_max_chars"]
    retrieval_mode: str = _MEMORY["retrieval_mode"]
    child_chars: int = _MEMORY["child_chars"]
    child_overlap_chars: int = _MEMORY["child_overlap_chars"]
    lexical_candidates: int = _MEMORY["lexical_candidates"]
    semantic_candidates: int = _MEMORY["semantic_candidates"]
    rrf_k: int = _MEMORY["rrf_k"]
    semantic_min_similarity: float = _MEMORY["semantic_min_similarity"]
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)


@dataclass(slots=True)
class EvergreenConfig:
    enabled: bool = _EVERGREEN["enabled"]
    max_items: int = _EVERGREEN["max_items"]
    max_chars: int = _EVERGREEN["max_chars"]


@dataclass(slots=True)
class AffectConfig:
    emotions_path: str = ""
    expected_emotion_version: str = ""
    mood_follow_hours: float = _UNSET
    mood_return_hours: float = _UNSET
    habituation_window_minutes: int = _UNSET
    habituation_factor: float = _UNSET
    silence_longing_per_hour: float = _UNSET
    silence_anxiety_per_hour: float = _UNSET
    silence_seeking_per_hour: float = _UNSET
    classification_fallback_seconds: int = _UNSET
    dimensions: dict[str, dict[str, float]] = field(default_factory=dict)
    label_patterns: dict[str, list[str]] = field(default_factory=dict)
    explicit_knobs: frozenset[str] = field(default=frozenset(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        explicit: set[str] = set()
        for name, default in _AFFECT_KNOBS.items():
            value = getattr(self, name)
            if value is _UNSET:
                setattr(self, name, default)
            else:
                explicit.add(name)
        self.explicit_knobs = frozenset(explicit)


@dataclass(slots=True)
class ProactiveConfig:
    enabled: bool = _PROACTIVE["enabled"]
    poll_interval_seconds: int = _PROACTIVE["poll_interval_seconds"]
    minimum_silence_minutes: int = _PROACTIVE["minimum_silence_minutes"]
    longing_threshold: float = _UNSET
    fear_threshold: float = _UNSET
    cooldown_minutes: int = _PROACTIVE["cooldown_minutes"]
    max_per_day: int = _PROACTIVE["max_per_day"]
    max_unanswered: int = _PROACTIVE["max_unanswered"]
    quiet_start_hour: int = _PROACTIVE["quiet_start_hour"]
    quiet_end_hour: int = _PROACTIVE["quiet_end_hour"]
    lease_seconds: int = _PROACTIVE["lease_seconds"]
    failed_retry_minutes: int = _PROACTIVE["failed_retry_minutes"]
    explicit_emotional: frozenset[str] = field(
        default=frozenset(), init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        explicit: set[str] = set()
        for name, default in _PROACTIVE_EMOTIONAL.items():
            value = getattr(self, name)
            if value is _UNSET:
                setattr(self, name, default)
            else:
                explicit.add(name)
        self.explicit_emotional = frozenset(explicit)


@dataclass(slots=True)
class AppConfig:
    data_dir: Path = Path(_DEFAULTS["data_dir"])
    host: str = _DEFAULTS["host"]
    port: int = _DEFAULTS["port"]
    timezone: str = _DEFAULTS["timezone"]
    api_token_env: str = _DEFAULTS["api_token_env"]
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    evergreen: EvergreenConfig = field(default_factory=EvergreenConfig)
    affect: AffectConfig = field(default_factory=lambda: AffectConfig())
    proactive: ProactiveConfig = field(default_factory=lambda: ProactiveConfig())

    @property
    def database_path(self) -> Path:
        return self.data_dir / "state.sqlite3"


# ---------------------------------------------------------------------------
# Strict parsing and validation
# ---------------------------------------------------------------------------


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


def _memory_section(raw: Any) -> MemoryConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("memory must be an object")
    hints = typing.get_type_hints(MemoryConfig)
    unknown = sorted(set(raw) - set(hints))
    if unknown:
        raise ValueError(f"unknown field(s) under memory: {unknown}")
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "embedding":
            kwargs["embedding"] = _strict_section(EmbeddingConfig, value, "memory.embedding")
        else:
            _validate_value(f"memory.{key}", value, hints[key])
            kwargs[key] = value
    return MemoryConfig(**kwargs)


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node: Any, deep: bool = False) -> dict:
        self.flatten_mapping(node)
        mapping: dict = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ValueError(f"duplicate key: {key!r}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _read_config_file(path: Path, is_json: bool) -> dict:
    text = path.read_text(encoding="utf-8")
    if is_json:
        return loads_strict(text, source=str(path))
    data = yaml.load(text, Loader=_UniqueKeyLoader)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _app_config_from_raw(raw: dict[str, Any], source_dir: Path, is_json: bool) -> AppConfig:
    allowed = set(typing.get_type_hints(AppConfig)) | {"emotions"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unknown top-level field(s): {unknown}")

    affect = _strict_section(AffectConfig, raw.get("affect"), "affect")
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
        if is_json and not data_dir.is_absolute():
            data_dir = source_dir / data_dir
        kwargs["data_dir"] = data_dir

    return AppConfig(
        **kwargs,
        upstream=_strict_section(UpstreamConfig, raw.get("upstream"), "upstream"),
        memory=_memory_section(raw.get("memory")),
        evergreen=_strict_section(EvergreenConfig, raw.get("evergreen"), "evergreen"),
        affect=affect,
        proactive=_strict_section(ProactiveConfig, raw.get("proactive"), "proactive"),
    )


def _resolve_config_path(explicit: str | Path | None) -> Path | None:
    if explicit is not None:
        path = Path(explicit)
        if not path.exists():
            raise FileNotFoundError(f"configuration file not found: {path}")
        return path
    env = os.getenv("COMPANION_GATEWAY_CONFIG")
    if env:
        path = Path(env)
        if not path.exists():
            raise FileNotFoundError(
                f"configuration file not found (from COMPANION_GATEWAY_CONFIG): {path}"
            )
        return path
    for candidate in ("config.json", "config.yaml"):
        path = Path(candidate)
        if path.exists():
            return path
    return None


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = _resolve_config_path(path)
    if config_path is None:
        return AppConfig()
    is_json = config_path.suffix.lower() == ".json"
    raw = _read_config_file(config_path, is_json)
    cfg = _app_config_from_raw(raw, config_path.parent, is_json=is_json)
    if not is_json:
        warnings.warn(
            f"{config_path} is a legacy YAML config and is deprecated; "
            "use config.json or run 'companion-gateway migrate-config "
            f"{config_path} config.json' to convert.",
            DeprecationWarning,
            stacklevel=2,
        )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _validate_packaged_defaults() -> None:
    """Validate packaged defaults.json against the same schema as user config."""
    hints = typing.get_type_hints(AppConfig)
    for key in ("data_dir", "host", "port", "timezone", "api_token_env"):
        _validate_value(f"packaged defaults {key}", _DEFAULTS[key], hints[key])
    _strict_section(UpstreamConfig, _DEFAULTS["upstream"], "packaged defaults upstream")
    _memory_section(_DEFAULTS["memory"])
    _strict_section(EvergreenConfig, _DEFAULTS["evergreen"], "packaged defaults evergreen")
    _strict_section(ProactiveConfig, _DEFAULTS["proactive"], "packaged defaults proactive")


_validate_packaged_defaults()


# ---------------------------------------------------------------------------
# Config migration (YAML/JSON to JSON)
# ---------------------------------------------------------------------------

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CREDENTIAL_PATTERNS = [
    re.compile(r"://[^/@\s]+:[^/@\s]+@"),
    re.compile(r"\bBearer\s+\S+", re.I),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (RSA|OPENSSH|EC|PRIVATE)"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
]

_MIGRATION_ORDER = [
    "emotions",
    "data_dir",
    "host",
    "port",
    "timezone",
    "api_token_env",
    "upstream",
    "memory",
    "evergreen",
    "affect",
    "proactive",
]


def _scrub_secrets(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("api_key_env", "api_token_env"):
                if isinstance(item, str) and item and not _ENV_NAME.match(item):
                    raise ValueError(
                        f"{path}.{key} must be an environment variable name, not a literal credential"
                    )
            _scrub_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scrub_secrets(item, f"{path}[{index}]")
    elif isinstance(value, str):
        for pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(value):
                raise ValueError(f"refusing to emit a possible secret value in {path or '<root>'}")


def _transform_for_migration(raw: dict[str, Any], src: Path, is_json: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _MIGRATION_ORDER:
        if key not in raw:
            continue
        if key == "data_dir":
            value = Path(str(raw["data_dir"])).expanduser()
            if not value.is_absolute():
                base = src.parent if is_json else Path.cwd()
                value = (base / value).resolve()
            out[key] = str(value)
        elif key == "emotions":
            emotions = dict(raw["emotions"])
            if "path" in emotions and emotions["path"]:
                value = Path(str(emotions["path"])).expanduser()
                if not value.is_absolute():
                    value = (src.parent / value).resolve()
                emotions["path"] = str(value)
            out[key] = emotions
        else:
            out[key] = copy.deepcopy(raw[key])
    return out


def migrate_config(source: str | Path, destination: str | Path, force: bool = False) -> dict:
    src = Path(source)
    if not src.exists():
        raise FileNotFoundError(f"source configuration not found: {src}")
    dest = Path(destination)
    if dest.exists() and not force:
        raise FileExistsError(f"destination already exists (use --force to overwrite): {dest}")
    is_json = src.suffix.lower() == ".json"
    raw = _read_config_file(src, is_json)
    # Validate before emitting; raises on unknown fields or bad types.
    _app_config_from_raw(raw, src.parent, is_json=is_json)
    emitted = _transform_for_migration(raw, src, is_json)
    _scrub_secrets(emitted)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(emitted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"source": str(src), "destination": str(dest)}