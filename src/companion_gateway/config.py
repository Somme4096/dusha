from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class UpstreamConfig:
    base_url: str = ""
    api_key_env: str = "UPSTREAM_API_KEY"
    timeout_seconds: float = 120.0


@dataclass(slots=True)
class EmbeddingConfig:
    base_url: str = ""
    api_key_env: str = "EMBEDDING_API_KEY"
    model: str = ""
    dimensions: int | None = None
    timeout_seconds: float = 10.0
    batch_size: int = 32
    backfill_interval_seconds: int = 10
    failure_cooldown_seconds: int = 60


@dataclass(slots=True)
class MemoryConfig:
    recent_messages: int = 8
    search_hits: int = 4
    context_messages: int = 1
    injection_max_chars: int = 12_000
    retrieval_mode: str = "lexical"
    chunk_max_chars: int = 800
    chunk_overlap_chars: int = 120
    lexical_candidates: int = 24
    semantic_candidates: int = 24
    rrf_k: int = 60


@dataclass(slots=True)
class EvergreenConfig:
    enabled: bool = True
    max_items: int = 32
    max_chars: int = 4_000


@dataclass(slots=True)
class AffectConfig:
    mood_follow_hours: float = 12.0
    mood_return_hours: float = 72.0
    habituation_window_minutes: int = 15
    habituation_factor: float = 0.7
    silence_longing_per_hour: float = 0.04
    silence_anxiety_per_hour: float = 0.02
    silence_seeking_per_hour: float = 0.02
    dimensions: dict[str, dict[str, float]] = field(default_factory=dict)
    label_patterns: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class ProactiveConfig:
    enabled: bool = True
    poll_interval_seconds: int = 60
    min_silence_minutes: int = 180
    longing_threshold: float = 0.48
    fear_threshold: float = 0.55
    cooldown_minutes: int = 360
    max_per_day: int = 2
    max_unanswered: int = 2
    quiet_start_hour: int = 1
    quiet_end_hour: int = 8
    lease_seconds: int = 120
    retry_delay_minutes: int = 15


@dataclass(slots=True)
class AppConfig:
    data_dir: Path = Path("./data")
    host: str = "127.0.0.1"
    port: int = 8765
    timezone: str = "Asia/Taipei"
    api_token_env: str = ""
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    evergreen: EvergreenConfig = field(default_factory=EvergreenConfig)
    affect: AffectConfig = field(default_factory=AffectConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "state.sqlite3"


DEFAULT_LABEL_PATTERNS = {
    "fear_death": [
        "suicide",
        "kill myself",
        "might die",
        "going to die",
        "life-threatening",
        "自殺",
        "死にたい",
        "死ぬかも",
        "要死了",
        "想死",
    ],
    "fear_separation": [
        "leave me",
        "abandon",
        "never come back",
        "goodbye forever",
        "置いていか",
        "見捨て",
        "もう会えない",
        "不要我",
        "永远离开",
    ],
    "fear_concern": [
        "are you safe",
        "be careful",
        "worried about you",
        "something happened to you",
        "無事",
        "気をつけて",
        "心配して",
        "担心你",
        "小心安全",
    ],
    "fear_general": [
        "afraid",
        "scared",
        "terrified",
        "frightened",
        "怖い",
        "恐い",
        "害怕",
        "恐惧",
    ],
    "hostile": ["hate you", "shut up", "worthless", "idiot", "うるさい", "嫌い", "闭嘴", "废物"],
    "conflict": ["we are fighting", "argument", "you hurt me", "喧嘩", "言い争い", "吵架"],
    "struggling": [
        "cannot cope",
        "can't cope",
        "exhausted",
        "overwhelmed",
        "burned out",
        "限界",
        "疲れ果て",
        "撑不住",
        "累死了",
    ],
    "reassuring": [
        "it will be okay",
        "i am here",
        "you are safe",
        "大丈夫",
        "そばにいる",
        "没事的",
        "我在",
    ],
    "affectionate": ["love you", "miss you", "dear", "大好き", "愛して", "想你", "爱你"],
    "playful": ["just kidding", "teasing", "haha", "lol", "冗談", "哈哈"],
    "vulnerable": ["i feel fragile", "insecure", "i need you", "不安", "寂しい", "没有安全感"],
    "cold": ["do not talk to me", "leave me alone", "話したくない", "别烦我"],
    "distant": ["busy now", "later", "not now", "今忙しい", "待会儿"],
    "intimate_reference": ["kiss", "hug", "抱きしめ", "キス", "亲吻", "拥抱"],
}


def _section(cls: type[Any], raw: dict[str, Any], name: str) -> Any:
    values = dict(raw.get(name) or {})
    fields = getattr(cls, "__dataclass_fields__", {})
    return cls(**{key: value for key, value in values.items() if key in fields})


def _migrate_config(raw: dict[str, Any]) -> bool:
    """Consume obsolete keys once; normal configuration loading uses only current names."""
    changed = False
    renames = {
        "memory": {"child_chars": "chunk_max_chars", "child_overlap_chars": "chunk_overlap_chars"},
        "proactive": {
            "minimum_silence_minutes": "min_silence_minutes",
            "failed_retry_minutes": "retry_delay_minutes",
        },
    }
    for section, keys in renames.items():
        values = raw.get(section) or {}
        for old, new in keys.items():
            if old in values:
                values.setdefault(new, values.pop(old))
                changed = True
    memory = raw.get("memory") or {}
    if "embedding" in memory:
        raw.setdefault("embedding", memory.pop("embedding"))
        changed = True
    for section in ("memory", "affect"):
        values = raw.get(section) or {}
        for key in ("semantic_enabled", "semantic_min_similarity"):
            if key in values:
                del values[key]
                changed = True
    return changed


def _save_migrated_config(path: Path, raw: dict[str, Any]) -> None:
    # Resolve symlinks and replace atomically so a failed write leaves the original intact.
    path = path.resolve()
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            yaml.safe_dump(raw, stream, sort_keys=False, allow_unicode=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _expand_path(path: str | Path) -> Path:
    """Expand shell-style user and environment references in a config path."""
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


def _config_path(path: str | Path | None) -> Path | None:
    if path is not None:
        # An explicitly requested path remains authoritative even when missing;
        # this preserves the previous defaults-on-missing-path behavior.
        return _expand_path(path)

    configured_path = os.getenv("COMPANION_GATEWAY_CONFIG")
    if configured_path is not None:
        # The environment variable is also authoritative when its path is
        # missing, for the same backwards-compatible behavior as above.
        return _expand_path(configured_path)

    xdg_config_home = os.getenv("XDG_CONFIG_HOME") or "~/.config"
    user_path = _expand_path(xdg_config_home) / "companion-gateway" / "config.yaml"
    if user_path.exists():
        return user_path

    cwd_path = Path("config.yaml")
    if cwd_path.exists():
        return cwd_path
    return None


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config using explicit, environment, user, then cwd precedence.

    Explicit and environment paths remain authoritative when missing, while
    discovered user and cwd candidates are considered only when they exist.
    """
    config_path = _config_path(path)
    raw: dict[str, Any] = {}
    if config_path is not None and config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    migrated = _migrate_config(raw)
    cfg = AppConfig(
        data_dir=Path(raw.get("data_dir", "./data")).expanduser(),
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 8765)),
        timezone=str(raw.get("timezone", "Asia/Taipei")),
        api_token_env=str(raw.get("api_token_env", "")),
        upstream=_section(UpstreamConfig, raw, "upstream"),
        embedding=_section(EmbeddingConfig, raw, "embedding"),
        memory=_section(MemoryConfig, raw, "memory"),
        evergreen=_section(EvergreenConfig, raw, "evergreen"),
        affect=_section(AffectConfig, raw, "affect"),
        proactive=_section(ProactiveConfig, raw, "proactive"),
    )
    patterns = dict(DEFAULT_LABEL_PATTERNS)
    patterns.update(cfg.affect.label_patterns)
    cfg.affect.label_patterns = patterns
    if migrated and config_path is not None:
        _save_migrated_config(config_path, raw)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
