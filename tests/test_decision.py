"""Decision plugin loading, contract, and engine integration."""

from __future__ import annotations

import inspect
import json
import sys
import threading
from datetime import UTC, datetime

import pytest

from companion_gateway import emotions
from companion_gateway import prompts as prompts_module
from companion_gateway.config import AffectConfig, DecisionConfig, PromptsConfig
from companion_gateway.decision import _load_module, _module_key

NOW = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)


def _write_mods(tmp_path, name, body):
    mods = tmp_path / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    (mods / f"{name}.py").write_text(body, encoding="utf-8")
    return mods


def _prompts_overlay(tmp_path, decision_instruction):
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps({"decision_instruction": decision_instruction}), encoding="utf-8")
    return PromptsConfig(path=str(path))


def _ingest(service, content, external_id="e1"):
    return service.ingest_message(
        harness="api", conversation_id="one", role="user", content=content,
        external_id=external_id, occurred_at=NOW,
    )


def _count_decisions(service):
    with service.database.connect() as db:
        return db.execute("SELECT COUNT(*) FROM affect_decisions").fetchone()[0]


def test_plugin_loads_from_env_mods_dir(tmp_path, svc, monkeypatch):
    mods = _write_mods(
        tmp_path, "chooser",
        "from companion_gateway.decision import DecisionResult\n"
        "def decide(request, options):\n"
        "    return DecisionResult(emotion='fear')\n",
    )
    monkeypatch.setenv("COMPANION_GATEWAY_MODS_DIR", str(mods))
    service = svc(decision=DecisionConfig(module="chooser", increment=0.25))
    assert service.decision.enabled is True
    stored = _ingest(service, "hello")
    assert stored["affect"]["emotion"] == "fear"
    assert stored["affect"]["state"]["base"]["fear"] == 0.25
    assert _count_decisions(service) == 1


def test_env_mods_dir_overrides_config_mods_dir(tmp_path, svc, monkeypatch):
    configured = _write_mods(
        tmp_path / "configured", "pick",
        "from companion_gateway.decision import DecisionResult\n"
        "def decide(request, options):\n"
        "    return DecisionResult(emotion='contentment')\n",
    )
    env_mods = _write_mods(
        tmp_path / "env", "pick",
        "from companion_gateway.decision import DecisionResult\n"
        "def decide(request, options):\n"
        "    return DecisionResult(emotion='fear')\n",
    )
    monkeypatch.delenv("COMPANION_GATEWAY_MODS_DIR", raising=False)
    service = svc(decision=DecisionConfig(module="pick", mods_dir=str(configured), increment=0.5))
    assert _ingest(service, "hello", "cfg")["affect"]["emotion"] == "contentment"

    monkeypatch.setenv("COMPANION_GATEWAY_MODS_DIR", str(env_mods))
    service = svc(decision=DecisionConfig(module="pick", mods_dir=str(configured), increment=0.5))
    assert _ingest(service, "hello", "env")["affect"]["emotion"] == "fear"


def test_plugin_chooses_custom_dimension_from_resolved_emotions(tmp_path, svc, monkeypatch):
    base = emotions.default_emotions()
    base["emotion_version"] = "custom-dim"
    base["dimensions"]["curiosity"] = {"neutral": 0.0, "floor": 0.0, "tau": 5}
    path = tmp_path / "emotions.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    mods = _write_mods(
        tmp_path, "pick_curiosity",
        "from companion_gateway.decision import DecisionResult\n"
        "def decide(request, options):\n"
        "    return DecisionResult(emotion='curiosity') if 'curiosity' in request.emotions else None\n",
    )
    monkeypatch.setenv("COMPANION_GATEWAY_MODS_DIR", str(mods))
    service = svc(
        affect=AffectConfig(emotions_path=str(path)),
        decision=DecisionConfig(module="pick_curiosity", increment=0.3),
    )
    stored = _ingest(service, "hello")
    assert stored["affect"]["emotion"] == "curiosity"
    assert stored["affect"]["state"]["base"]["curiosity"] == 0.3
    # The decision only touches the selected dimension.
    assert stored["affect"]["state"]["base"]["fear"] == 0.0


def test_plugin_receives_message_instruction_dimensions_and_copied_state(tmp_path, svc, monkeypatch):
    capture = tmp_path / "capture.json"
    mods = _write_mods(
        tmp_path, "capture",
        "import json\n"
        "from companion_gateway.decision import DecisionResult\n"
        "def decide(request, options):\n"
        "    request.emotions['fear']['neutral'] = 999\n"
        "    request.state['base']['fear'] = 999\n"
        "    with open(options['capture'], 'w') as handle:\n"
        "        json.dump({'message': request.message, 'instruction': request.instruction,\n"
        "                   'emotions': sorted(request.emotions), 'state': request.state,\n"
        "                   'options': options}, handle)\n"
        "    return DecisionResult(emotion='contentment')\n",
    )
    monkeypatch.setenv("COMPANION_GATEWAY_MODS_DIR", str(mods))
    prompts = _prompts_overlay(tmp_path, "SENTINEL_DECISION_INSTRUCTION")
    service = svc(
        prompts=prompts,
        decision=DecisionConfig(module="capture", options={"capture": str(capture)}, increment=0.4),
    )
    stored = _ingest(service, "remember the amber window")
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["message"] == "remember the amber window"
    assert payload["instruction"] == "SENTINEL_DECISION_INSTRUCTION"
    assert "fear" in payload["emotions"] and "contentment" in payload["emotions"]
    assert set(payload["state"]) >= {"base", "mood", "last_updated_at"}
    assert payload["options"]["capture"] == str(capture)
    # The plugin's mutations of the copies never reach the engine.
    assert service.affect.spec["fear"]["neutral"] == 0.0
    assert stored["affect"]["state"]["base"]["fear"] == 0.0
    assert stored["affect"]["state"]["base"]["contentment"] == 0.75


def test_absent_plugin_makes_no_decision_but_records_contact(svc):
    service = svc()
    assert service.decision.enabled is False
    stored = _ingest(service, "hello")
    assert stored["affect"] is None
    assert _count_decisions(service) == 0
    state = service.affect.status(now=NOW)
    assert state["base"]["fear"] == 0.0
    assert state["last_user_message_at"] is not None


def test_raising_plugin_makes_no_decision(tmp_path, svc, monkeypatch):
    mods = _write_mods(
        tmp_path, "boom",
        "def decide(request, options):\n"
        "    raise RuntimeError('boom')\n",
    )
    monkeypatch.setenv("COMPANION_GATEWAY_MODS_DIR", str(mods))
    service = svc(decision=DecisionConfig(module="boom", increment=0.5))
    assert service.decision.enabled is True
    stored = _ingest(service, "hello")
    assert stored["affect"] is None
    assert _count_decisions(service) == 0
    assert service.affect.status(now=NOW)["base"]["fear"] == 0.0


@pytest.mark.parametrize(
    "body",
    [
        "def not_decide(request, options):\n    return None\n",
        "decide = 5\n",
        "import os\n",
    ],
)
def test_missing_or_noncallable_decide_disables_plugin(tmp_path, svc, monkeypatch, body):
    mods = _write_mods(tmp_path, "broken", body)
    monkeypatch.setenv("COMPANION_GATEWAY_MODS_DIR", str(mods))
    service = svc(decision=DecisionConfig(module="broken", increment=0.5))
    assert service.decision.enabled is False
    assert _ingest(service, "hello")["affect"] is None
    assert _count_decisions(service) == 0


@pytest.mark.parametrize(
    "result",
    ["None", "'not-a-dimension'", "{'emotion': 'fear'}", "5", "DecisionResult(emotion='bogus')"],
)
def test_malformed_result_makes_no_decision(tmp_path, svc, monkeypatch, result):
    mods = _write_mods(
        tmp_path, "bad_result",
        "from companion_gateway.decision import DecisionResult\n"
        f"def decide(request, options):\n    return {result}\n",
    )
    monkeypatch.setenv("COMPANION_GATEWAY_MODS_DIR", str(mods))
    service = svc(decision=DecisionConfig(module="bad_result", increment=0.5))
    assert service.decision.enabled is True
    assert _ingest(service, "hello")["affect"] is None
    assert _count_decisions(service) == 0
    assert service.affect.status(now=NOW)["base"]["fear"] == 0.0


@pytest.mark.parametrize("module", ["../outside", "/tmp/outside", "a/b", "with space"])
def test_module_name_cannot_traverse(tmp_path, svc, monkeypatch, module):
    mods = tmp_path / "mods"
    mods.mkdir(exist_ok=True)
    (tmp_path / "outside.py").write_text(
        "from companion_gateway.decision import DecisionResult\n"
        "def decide(request, options):\n    return DecisionResult(emotion='fear')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COMPANION_GATEWAY_MODS_DIR", str(mods))
    service = svc(decision=DecisionConfig(module=module))
    assert service.decision.enabled is False


def test_plugin_can_abstain_with_none(tmp_path, svc, monkeypatch):
    mods = _write_mods(
        tmp_path, "abstain",
        "def decide(request, options):\n"
        "    return None\n",
    )
    monkeypatch.setenv("COMPANION_GATEWAY_MODS_DIR", str(mods))
    service = svc(decision=DecisionConfig(module="abstain", increment=0.5))
    assert service.decision.enabled is True
    assert _ingest(service, "hello")["affect"] is None
    assert _count_decisions(service) == 0


def test_loader_supports_future_dataclass_annotations_and_cleans_failed_import(tmp_path):
    from companion_gateway.config import AppConfig
    from companion_gateway.decision import load_decider

    mods = _write_mods(
        tmp_path,
        "typed",
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "@dataclass\nclass PluginValue:\n    emotion: str\n"
        "def decide(request, options):\n    return PluginValue('fear')\n",
    )
    config = AppConfig(data_dir=tmp_path / "data", decision=DecisionConfig(module="typed"))
    config.decision.mods_dir = str(mods)
    decide = load_decider(config)
    assert decide is not None
    assert inspect.signature(decide).return_annotation is inspect.Signature.empty

    broken = _write_mods(tmp_path / "broken", "typed", "raise RuntimeError('broken')\n")
    path = (broken / "typed.py").resolve()
    key = _module_key("typed", path)
    with pytest.raises(RuntimeError):
        _load_module("typed", broken)
    assert key not in sys.modules


def test_same_named_plugins_in_different_directories_are_isolated(tmp_path):
    from types import SimpleNamespace

    from companion_gateway.decision import load_decider

    left = _write_mods(
        tmp_path / "left", "same", "VALUE = 'left'\ndef decide(request, options): return VALUE\n"
    )
    right = _write_mods(
        tmp_path / "right", "same", "VALUE = 'right'\ndef decide(request, options): return VALUE\n"
    )
    left_decide = load_decider(SimpleNamespace(decision=SimpleNamespace(module="same", mods_dir=str(left))))
    right_decide = load_decider(SimpleNamespace(decision=SimpleNamespace(module="same", mods_dir=str(right))))
    assert left_decide(None, {}) == "left"
    assert right_decide(None, {}) == "right"
    assert left_decide.__module__ != right_decide.__module__


def test_decider_options_are_copied_for_each_call(tmp_path, svc, monkeypatch):
    mods = _write_mods(
        tmp_path,
        "mutate_options",
        "from companion_gateway.decision import DecisionResult\n"
        "def decide(request, options):\n"
        "    options['nested']['changed'] = True\n"
        "    return DecisionResult('fear')\n",
    )
    monkeypatch.setenv("COMPANION_GATEWAY_MODS_DIR", str(mods))
    service = svc(decision=DecisionConfig(module="mutate_options", options={"nested": {}}))
    assert service.decision.evaluate(message="x", emotions=service.affect.spec,
                                     state=service.affect.initial_state(), instruction="") == "fear"
    assert service.decision.options == {"nested": {}}


def test_blocking_decider_does_not_block_status_and_preserves_intervening_write(svc):
    service = svc()
    entered = threading.Event()
    release = threading.Event()
    result = []

    def decider(**_):
        entered.set()
        assert release.wait(2)
        return "fear"

    worker = threading.Thread(
        target=lambda: result.append(service.affect.record_user_message(
            message="hello", source_message_id=None, decider=decider, instruction="", now=NOW
        ))
    )
    worker.start()
    assert entered.wait(2)
    status_done = threading.Event()
    status_result = []
    status_thread = threading.Thread(
        target=lambda: (status_result.append(service.affect.status(now=NOW)), status_done.set())
    )
    status_thread.start()
    assert status_done.wait(2)
    service.affect.on_proactive_sent(now=NOW)
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert result[0]["emotion"] == "fear"
    assert status_result
    assert service.affect.status(now=NOW)["base"]["anxiety"] > 0


def test_default_decision_instruction_targets_companion_emotion():
    instruction = prompts_module.default_prompts()["decision_instruction"]
    assert "companion's own" in instruction
    assert "not the speaker's emotion" in instruction


async def test_api_ingest_applies_plugin_decision(tmp_path, config, monkeypatch):
    import httpx

    from companion_gateway import api

    mods = _write_mods(
        tmp_path, "chooser",
        "from companion_gateway.decision import DecisionResult\n"
        "def decide(request, options):\n"
        "    return DecisionResult(emotion='fear')\n",
    )
    monkeypatch.setenv("COMPANION_GATEWAY_MODS_DIR", str(mods))
    config.decision = DecisionConfig(module="chooser", increment=0.3)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/state/v1/messages",
            json={"harness": "api", "conversation_id": "one", "role": "user",
                  "content": "hello", "external_id": "api-decision"},
        )
    assert response.status_code == 200
    affect = response.json()["affect"]
    assert affect["emotion"] == "fear"
    assert affect["state"]["base"]["fear"] == 0.3
