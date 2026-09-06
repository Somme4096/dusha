from __future__ import annotations

import os
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
class MemoryConfig:
    recent_messages: int = 8
    search_hits: int = 4
    context_messages: int = 1
    injection_max_chars: int = 12_000


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
    minimum_silence_minutes: int = 180
    longing_threshold: float = 0.48
    fear_threshold: float = 0.55
    cooldown_minutes: int = 360
    max_per_day: int = 2
    max_unanswered: int = 2
    quiet_start_hour: int = 1
    quiet_end_hour: int = 8
    lease_seconds: int = 120
    failed_retry_minutes: int = 15


@dataclass(slots=True)
class AppConfig:
    data_dir: Path = Path("./data")
    host: str = "127.0.0.1"
    port: int = 8765
    timezone: str = "Asia/Taipei"
    default_companion_id: str = "sophia"
    api_token_env: str = ""
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
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
    values = raw.get(name) or {}
    fields = getattr(cls, "__dataclass_fields__", {})
    return cls(**{key: value for key, value in values.items() if key in fields})


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path or os.getenv("COMPANION_GATEWAY_CONFIG", "config.yaml"))
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    cfg = AppConfig(
        data_dir=Path(raw.get("data_dir", "./data")).expanduser(),
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 8765)),
        timezone=str(raw.get("timezone", "Asia/Taipei")),
        default_companion_id=str(raw.get("default_companion_id", "sophia")),
        api_token_env=str(raw.get("api_token_env", "")),
        upstream=_section(UpstreamConfig, raw, "upstream"),
        memory=_section(MemoryConfig, raw, "memory"),
        affect=_section(AffectConfig, raw, "affect"),
        proactive=_section(ProactiveConfig, raw, "proactive"),
    )
    patterns = dict(DEFAULT_LABEL_PATTERNS)
    patterns.update(cfg.affect.label_patterns)
    cfg.affect.label_patterns = patterns
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
