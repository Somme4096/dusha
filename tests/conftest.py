from __future__ import annotations

from pathlib import Path

import pytest

from companion_gateway.config import (
    AffectConfig,
    AppConfig,
    EvergreenConfig,
    MemoryConfig,
    ProactiveConfig,
    UpstreamConfig,
)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path,
        timezone="Asia/Taipei",
        upstream=UpstreamConfig(),
        memory=MemoryConfig(recent_messages=2, search_hits=4, context_messages=1, injection_max_chars=20_000),
        evergreen=EvergreenConfig(enabled=True, max_items=32, max_chars=4_000),
        affect=AffectConfig(),
        proactive=ProactiveConfig(
            enabled=True,
            poll_interval_seconds=60,
            minimum_silence_minutes=60,
            longing_threshold=0.31,
            fear_threshold=0.55,
            cooldown_minutes=360,
            max_per_day=2,
            max_unanswered=1,
            quiet_start_hour=0,
            quiet_end_hour=0,
            lease_seconds=120,
            failed_retry_minutes=15,
        ),
    )
