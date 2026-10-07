from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
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
_EMBEDDING = _DEFAULTS.get("embedding", _MEMORY["embedding"])
_EVERGREEN = _DEFAULTS["evergreen"]
_DECISION = _DEFAULTS["decision"]
_STORAGE = _DEFAULTS["storage"]
_MEMORY_PLUGIN = _DEFAULTS.get("memory_plugin", {
    "module": "",
    "options": {},
    "mods_dir": "",
    "timeout_seconds": 15.0,
    "ingest_batch_size": 50,
    "ingest_backfill_interval_seconds": 30,
})
_AFFECT_KNOBS = _EMOTIONS["affect"]
_LEGACY_SILENCE_RATE = re.compile(r"silence_(.+)_per_hour")
_LEGACY_THRESHOLD = re.compile(r"(.+)_threshold")
_PROACTIVE = _DEFAULTS["proactive"]
_IDENTITY_PROMPT = _DEFAULTS["identity_prompt"]
_PROMPTS = _DEFAULTS["prompts"]


def default_config_dir() -> Path:
    base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "companion-gateway"

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
    chunk_max_chars: int = _MEMORY["chunk_max_chars"]
    chunk_overlap_chars: int = _MEMORY["chunk_overlap_chars"]
    lexical_candidates: int = _MEMORY["lexical_candidates"]
    semantic_candidates: int = _MEMORY["semantic_candidates"]
    rrf_k: int = _MEMORY["rrf_k"]
    plugin_context_max_chars: int = _MEMORY["plugin_context_max_chars"]
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)

    @property
    def child_chars(self) -> int:
        return self.chunk_max_chars

    @property
    def child_overlap_chars(self) -> int:
        return self.chunk_overlap_chars


@dataclass(slots=True)
class EvergreenConfig:
    enabled: bool = _EVERGREEN["enabled"]
    max_items: int = _EVERGREEN["max_items"]
    max_chars: int = _EVERGREEN["max_chars"]


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
class MemoryPluginConfig:
    module: str = _MEMORY_PLUGIN["module"]
    options: dict[str, Any] = field(default_factory=lambda: dict(_MEMORY_PLUGIN["options"]))
    mods_dir: str = _MEMORY_PLUGIN["mods_dir"]
    timeout_seconds: float = _MEMORY_PLUGIN["timeout_seconds"]
    ingest_batch_size: int = _MEMORY_PLUGIN["ingest_batch_size"]
    ingest_backfill_interval_seconds: int = _MEMORY_PLUGIN["ingest_backfill_interval_seconds"]

    def __post_init__(self) -> None:
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

    @property
    def minimum_silence_minutes(self) -> int:
        return self.min_silence_minutes

    @property
    def failed_retry_minutes(self) -> int:
        return self.retry_delay_minutes


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
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    evergreen: EvergreenConfig = field(default_factory=EvergreenConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    affect: AffectConfig = field(default_factory=lambda: AffectConfig())
    proactive: ProactiveConfig = field(default_factory=lambda: ProactiveConfig())
    decision: DecisionConfig = field(default_factory=lambda: DecisionConfig())
    memory_plugin: MemoryPluginConfig = field(default_factory=lambda: MemoryPluginConfig())

    def __post_init__(self) -> None:
        if self.memory_plugin.module and not self.storage.enabled:
            raise ValueError("memory_plugin.module requires storage.enabled")

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


def _migrate_config(raw: dict[str, Any]) -> bool:
    changed = False
    memory = raw.get("memory")
    if isinstance(memory, dict):
        for old, new in (
            ("child_chars", "chunk_max_chars"),
            ("child_overlap_chars", "chunk_overlap_chars"),
        ):
            if old in memory:
                memory.setdefault(new, memory.pop(old))
                changed = True
        if "embedding" in memory:
            raw.setdefault("embedding", memory.pop("embedding"))
            changed = True
        for key in ("semantic_enabled", "semantic_min_similarity"):
            if key in memory:
                del memory[key]
                changed = True
    proactive = raw.get("proactive")
    if isinstance(proactive, dict):
        for old, new in (
            ("minimum_silence_minutes", "min_silence_minutes"),
            ("failed_retry_minutes", "retry_delay_minutes"),
        ):
            if old in proactive:
                proactive.setdefault(new, proactive.pop(old))
                changed = True
        for key in [key for key in proactive if isinstance(key, str)]:
            match = _LEGACY_THRESHOLD.fullmatch(key)
            if match:
                thresholds = proactive.setdefault("thresholds", {})
                if isinstance(thresholds, dict):
                    thresholds.setdefault(match.group(1), proactive.pop(key))
                    changed = True
    affect = raw.get("affect")
    if isinstance(affect, dict):
        for key in [key for key in affect if isinstance(key, str)]:
            match = _LEGACY_SILENCE_RATE.fullmatch(key)
            if match:
                silence = affect.setdefault("silence", {})
                rule = silence.setdefault(match.group(1), {}) if isinstance(silence, dict) else None
                if isinstance(rule, dict):
                    rule.setdefault("rate_per_hour", affect.pop(key))
                    changed = True
        for key in ("semantic_enabled", "semantic_min_similarity"):
            if key in affect:
                del affect[key]
                changed = True
    return changed


def _save_migrated_config(path: Path, raw: dict[str, Any]) -> None:
    path = path.resolve()
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(raw, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


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

    embedding = _strict_section(EmbeddingConfig, raw.get("embedding"), "embedding")
    memory = _memory_section(raw.get("memory"))
    memory.embedding = embedding

    return AppConfig(
        **kwargs,
        identity_prompt=identity_prompt,
        prompts=prompts,
        upstream=_strict_section(UpstreamConfig, raw.get("upstream"), "upstream"),
        embedding=embedding,
        api_openai=_strict_section(ApiOpenAIConfig, raw.get("api_openai"), "api_openai"),
        memory=memory,
        evergreen=_strict_section(EvergreenConfig, raw.get("evergreen"), "evergreen"),
        storage=_strict_section(StorageConfig, raw.get("storage"), "storage"),
        affect=affect,
        proactive=_strict_section(ProactiveConfig, raw.get("proactive"), "proactive"),
        decision=decision,
        memory_plugin=memory_plugin,
    )


def _resolve_config_path(explicit: str | Path | None) -> Path | None:
    if explicit is not None:
        path = Path(os.path.expandvars(os.path.expanduser(str(explicit))))
        if path.suffix.lower() != ".json":
            raise ValueError(f"unsupported configuration format: {path}. Use a JSON file")
        if not path.exists():
            raise FileNotFoundError(f"configuration file not found: {path}")
        return path
    env = os.getenv("COMPANION_GATEWAY_CONFIG")
    if env:
        path = Path(os.path.expandvars(os.path.expanduser(env)))
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
    migrated = _migrate_config(raw)
    cfg = _app_config_from_raw(raw, config_path.parent)
    if migrated:
        _save_migrated_config(config_path, raw)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _validate_packaged_defaults() -> None:
    hints = typing.get_type_hints(AppConfig)
    for key in ("data_dir", "host", "port", "timezone", "api_token_env"):
        _validate_value(f"packaged defaults {key}", _DEFAULTS[key], hints[key])
    _strict_section(UpstreamConfig, _DEFAULTS["upstream"], "packaged defaults upstream")
    _strict_section(
        EmbeddingConfig, _DEFAULTS.get("embedding", _EMBEDDING), "packaged defaults embedding"
    )
    _strict_section(ApiOpenAIConfig, _DEFAULTS["api_openai"], "packaged defaults api_openai")
    _memory_section(_DEFAULTS["memory"])
    _strict_section(EvergreenConfig, _DEFAULTS["evergreen"], "packaged defaults evergreen")
    _strict_section(StorageConfig, _DEFAULTS["storage"], "packaged defaults storage")
    _strict_section(ProactiveConfig, _DEFAULTS["proactive"], "packaged defaults proactive")
    _strict_section(DecisionConfig, _DEFAULTS["decision"], "packaged defaults decision")
    _strict_section(
        MemoryPluginConfig, _DEFAULTS.get("memory_plugin", _MEMORY_PLUGIN),
        "packaged defaults memory_plugin",
    )
    _strict_section(
        IdentityPromptConfig, _DEFAULTS["identity_prompt"], "packaged defaults identity_prompt"
    )
    _strict_section(PromptsConfig, _DEFAULTS["prompts"], "packaged defaults prompts")


_validate_packaged_defaults()
