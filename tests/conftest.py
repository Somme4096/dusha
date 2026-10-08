from __future__ import annotations

import json
from pathlib import Path

import pytest

from dusha.config import (
    AffectConfig,
    AppConfig,
    EvergreenConfig,
    MemoryConfig,
    ProactiveConfig,
    UpstreamConfig,
)
from dusha.service import CompanionService


@pytest.fixture(autouse=True)
def isolated_config_root(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))
    for name in ("DUSHA_HOME", "DUSHA_COMPANION", "DUSHA_MODS_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def write_json():
    def write(path, value):
        Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return Path(path)

    return write


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
            min_silence_minutes=60,
            thresholds={"longing": 0.31, "fear": 0.55},
            cooldown_minutes=360,
            max_per_day=2,
            max_unanswered=1,
            quiet_start_hour=0,
            quiet_end_hour=0,
            lease_seconds=120,
            retry_delay_minutes=15,
        ),
    )


@pytest.fixture
def svc(tmp_path: Path, config: AppConfig):
    services: list[CompanionService] = []
    counter = 0

    def factory(*, cfg: AppConfig | None = None, **sections) -> CompanionService:
        nonlocal counter
        counter += 1
        if cfg is not None:
            app = cfg
        else:
            sections = {k: v for k, v in sections.items() if v is not None}
            app = (
                AppConfig(data_dir=tmp_path / f"d{counter}", timezone="Asia/Taipei", **sections)
                if sections
                else config
            )
        service = CompanionService(app)
        services.append(service)
        return service

    yield factory
    for service in services:
        service.close()


class EmbeddingStub:
    def __init__(self):
        from dusha.emotions import default_emotions

        prototypes = default_emotions()["appraisal"]["prototypes"]
        self._labels = list(prototypes)
        self._texts = {spec["text"]: label for label, spec in prototypes.items()}
        self.says: dict[str, str] = {}
        self.fail = False
        self.calls = 0
        self.url = ""

    def vector(self, text: str) -> list[float]:
        label = self._texts.get(text) or self.says.get(text)
        index = self._labels.index(label) if label else len(self._labels)
        return [1.0 if position == index else 0.0 for position in range(len(self._labels) + 1)]


@pytest.fixture
def embedding_stub():
    # Stands in for the external embedding provider.
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    stub = EmbeddingStub()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            texts = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["input"]
            stub.calls += 1
            data = [{"index": index, "embedding": stub.vector(text)} for index, text in enumerate(texts)]
            body = json.dumps({"data": data}).encode()
            self.send_response(500 if stub.fail else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stub.url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield stub
    server.shutdown()
    server.server_close()
