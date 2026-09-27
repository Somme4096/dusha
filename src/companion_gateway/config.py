from __future__ import annotations

import json
import math
import os
import types as _types
import typing
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
_EMBEDDING = _MEMORY["embedding"]
_EVERGREEN = _DEFAULTS["evergreen"]
_DECISION = _DEFAULTS["decision"]
_AFFECT_KNOBS = _EMOTIONS["affect"]
_PROACTIVE_EMOTIONAL = _EMOTIONS["proactive"]
_PROACTIVE = _DEFAULTS["proactive"]
_IDENTITY_PROMPT = _DEFAULTS["identity_prompt"]
_PROMPTS = _DEFAULTS["prompts"]


def default_config_dir() -> Path:
    return Path.home() / ".config" / "companion-gateway"

_UNSET: Any = _emotions.UNSET


def _fill_unset(config_obj: Any, knobs: dict[str, Any], explicit_attr: str) -> None:
    explicit: set[str] = set()
    for name, default in knobs.items():
        value = getattr(config_obj, name)
        if value is _UNSET:
            setattr(config_obj, name, default)
        else:
            explicit.add(name)
    setattr(config_obj, explicit_attr, frozenset(explicit))


@dataclass(slots=True)
class UpstreamConfig:
    base_url: str = _UPSTREAM["base_url"]
    api_key_env: str = _UPSTREAM["api_key_env"]
    timeout_seconds: float = _UPSTREAM["timeout_seconds"]


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
    silence_longing_per_hour: float = _UNSET
    silence_anxiety_per_hour: float = _UNSET
    silence_seeking_per_hour: float = _UNSET
    dimensions: dict[str, dict[str, float]] = field(default_factory=dict)
    explicit_knobs: frozenset[str] = field(default=frozenset(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _fill_unset(self, _AFFECT_KNOBS, "explicit_knobs")


@dataclass(slots=True)
class DecisionConfig:

    module: str = _DECISION["module"]
    options: dict[str, Any] = field(default_factory=lambda: dict(_DECISION["options"]))
    increment: float = _DECISION["increment"]
    mods_dir: str = _DECISION["mods_dir"]
    timeout_seconds: float = _DECISION["timeout_seconds"]

    def __post_init__(self) -> None:
        value = self.increment
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("decision.increment must be a finite number in (0, 1]")
        number = float(value)
        if not math.isfinite(number) or not 0 < number <= 1:
            raise ValueError("decision.increment must be a finite number in (0, 1]")
        self.increment = number
        timeout = self.timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("decision.timeout_seconds must be a finite positive number")
        timeout_number = float(timeout)
        if not math.isfinite(timeout_number) or timeout_number <= 0:
            raise ValueError("decision.timeout_seconds must be a finite positive number")
        self.timeout_seconds = timeout_number


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
        _fill_unset(self, _PROACTIVE_EMOTIONAL, "explicit_emotional")


@dataclass(slots=True)
class IdentityPromptConfig:
    path: str = _IDENTITY_PROMPT["path"]


@dataclass(slots=True)
class PromptsConfig:
    path: str = _PROMPTS["path"]


@dataclass(slots=True)
class AppConfig:
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
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    evergreen: EvergreenConfig = field(default_factory=EvergreenConfig)
    affect: AffectConfig = field(default_factory=lambda: AffectConfig())
    proactive: ProactiveConfig = field(default_factory=lambda: ProactiveConfig())
    decision: DecisionConfig = field(default_factory=lambda: DecisionConfig())

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


def _read_config_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return loads_strict(text, source=str(path))


def _path_section(cls: type[Any], raw: Any, name: str, source_dir: Path) -> Any:
    section = _strict_section(cls, raw, name)
    if section.path and not Path(section.path).is_absolute():
        section.path = str((source_dir / Path(section.path).expanduser()).resolve())
    return section


def _app_config_from_raw(raw: dict[str, Any], source_dir: Path) -> AppConfig:
    allowed = set(typing.get_type_hints(AppConfig)) | {"emotions"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unknown top-level field(s): {unknown}")

    affect = _strict_section(AffectConfig, raw.get("affect"), "affect")
    identity_prompt = _path_section(
        IdentityPromptConfig, raw.get("identity_prompt"), "identity_prompt", source_dir
    )
    prompts = _path_section(PromptsConfig, raw.get("prompts"), "prompts", source_dir)
    decision = _strict_section(DecisionConfig, raw.get("decision"), "decision")
    if decision.mods_dir and not Path(decision.mods_dir).is_absolute():
        decision.mods_dir = str((source_dir / Path(decision.mods_dir).expanduser()).resolve())
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

    return AppConfig(
        **kwargs,
        identity_prompt=identity_prompt,
        prompts=prompts,
        upstream=_strict_section(UpstreamConfig, raw.get("upstream"), "upstream"),
        api_openai=_strict_section(ApiOpenAIConfig, raw.get("api_openai"), "api_openai"),
        memory=_memory_section(raw.get("memory")),
        evergreen=_strict_section(EvergreenConfig, raw.get("evergreen"), "evergreen"),
        affect=affect,
        proactive=_strict_section(ProactiveConfig, raw.get("proactive"), "proactive"),
        decision=decision,
    )


def _resolve_config_path(explicit: str | Path | None) -> Path | None:
    if explicit is not None:
        path = Path(explicit)
        if path.suffix.lower() != ".json":
            raise ValueError(f"unsupported configuration format: {path}. Use a JSON file")
        if not path.exists():
            raise FileNotFoundError(f"configuration file not found: {path}")
        return path
    env = os.getenv("COMPANION_GATEWAY_CONFIG")
    if env:
        path = Path(env)
        if path.suffix.lower() != ".json":
            raise ValueError(f"unsupported configuration format: {path}. Use a JSON file")
        if not path.exists():
            raise FileNotFoundError(
                f"configuration file not found (from COMPANION_GATEWAY_CONFIG): {path}"
            )
        return path
    for name in ("config.json",):
        path = default_config_dir() / name
        if path.exists():
            return path
    for candidate in ("config.json",):
        path = Path(candidate)
        if path.exists():
            return path
    return None


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = _resolve_config_path(path)
    if config_path is None:
        return AppConfig()
    if config_path.suffix.lower() != ".json":
        raise ValueError(f"unsupported configuration format: {config_path}. Use a JSON file")
    raw = _read_config_file(config_path)
    cfg = _app_config_from_raw(raw, config_path.parent)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _validate_packaged_defaults() -> None:
    hints = typing.get_type_hints(AppConfig)
    for key in ("data_dir", "host", "port", "timezone", "api_token_env"):
        _validate_value(f"packaged defaults {key}", _DEFAULTS[key], hints[key])
    _strict_section(UpstreamConfig, _DEFAULTS["upstream"], "packaged defaults upstream")
    _strict_section(ApiOpenAIConfig, _DEFAULTS["api_openai"], "packaged defaults api_openai")
    _memory_section(_DEFAULTS["memory"])
    _strict_section(EvergreenConfig, _DEFAULTS["evergreen"], "packaged defaults evergreen")
    _strict_section(ProactiveConfig, _DEFAULTS["proactive"], "packaged defaults proactive")
    _strict_section(DecisionConfig, _DEFAULTS["decision"], "packaged defaults decision")
    _strict_section(
        IdentityPromptConfig, _DEFAULTS["identity_prompt"], "packaged defaults identity_prompt"
    )
    _strict_section(PromptsConfig, _DEFAULTS["prompts"], "packaged defaults prompts")


_validate_packaged_defaults()
