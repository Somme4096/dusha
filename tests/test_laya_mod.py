"""Contract tests for the externally hosted Laya decision mod.

The mod is loaded from ``examples/mods/laya.py`` exactly as the gateway loads
it from ``~/.config/companion-gateway/mods/``. HTTP is always mocked, so the
suite never touches a real endpoint or imports ``laya``/``torch``.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest

from companion_gateway.decision import DecisionRequest, DecisionResult

_MOD_PATH = Path(__file__).resolve().parents[1] / "examples" / "mods" / "laya.py"


def _load_laya():
    name = "_laya_mod_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _MOD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


laya = _load_laya()


def test_adapter_binds_the_real_core_contract():
    """No local stub: the adapter and tests use the shipped decision types."""

    import companion_gateway.decision as decision

    assert DecisionRequest is decision.DecisionRequest
    assert DecisionResult is decision.DecisionResult
    assert laya.DecisionRequest is decision.DecisionRequest
    assert laya.DecisionResult is decision.DecisionResult


def _response(status: int = 200, **kwargs) -> httpx.Response:
    request = httpx.Request("POST", "https://laya.example/v1/systemone")
    return httpx.Response(status, request=request, **kwargs)


def _choice_response(choice: str, type_: str = "choice") -> httpx.Response:
    return _response(
        json={
            "model": "laya-rl-agent",
            "answers": {"emotion": {"type": type_, "choice": choice}},
            "usage": {"input_tokens": 12, "output_tokens": 0},
        }
    )


def _request(**overrides) -> DecisionRequest:
    values = {
        "message": "hello there",
        "emotions": {"affectionate": {"description": "warm and close"}, "fear_separation": {}},
        "state": {"longing": 0.4},
        "instruction": "Pick the emotion that best fits.",
    }
    values.update(overrides)
    return DecisionRequest(**values)


class _Recorder:
    def __init__(self, response=None, error=None):
        self.calls: list[tuple[str, dict]] = []
        self._response = response
        self._error = error

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._error is not None:
            raise self._error
        return self._response

    @property
    def body(self) -> dict:
        return self.calls[-1][1]["json"]


@pytest.fixture
def post(monkeypatch):
    """Install a recording fake for the mod's ``httpx.post``."""

    def install(response=None, error=None) -> _Recorder:
        recorder = _Recorder(response=response, error=error)
        fake = types.SimpleNamespace(
            post=recorder,
            HTTPStatusError=httpx.HTTPStatusError,
            RequestError=httpx.RequestError,
        )
        monkeypatch.setattr(laya, "httpx", fake)
        return recorder

    return install


def test_request_shape_and_selected_emotion(post):
    recorder = post(_choice_response("affectionate"))
    request = _request()
    state_snapshot = copy.deepcopy(request.state)

    result = laya.decide(request, {"base_url": "https://laya.example/base/"})

    assert isinstance(result, DecisionResult)
    assert result.emotion == "affectionate"
    assert len(recorder.calls) == 1
    url, kwargs = recorder.calls[0]
    assert url == "https://laya.example/base/v1/systemone"
    assert kwargs["timeout"] == 10.0
    assert kwargs["headers"] is None
    assert kwargs["json"] == {
        "state": {"message": "hello there", "emotion_state": {"longing": 0.4}},
        "questions": {
            "emotion": {
                "type": "choice",
                "instructions": "Pick the emotion that best fits.",
                "criteria": {
                    "affectionate": "warm and close",
                    "fear_separation": "fear separation",
                },
            }
        },
    }
    assert "model" not in kwargs["json"]
    assert request.state == state_snapshot
    assert request.message == "hello there"


@pytest.mark.parametrize(
    "definition",
    [{}, {"description": None}, {"description": "   "}, {"description": 5}, "not-a-mapping"],
)
def test_criteria_falls_back_to_humanized_dimension(post, definition):
    recorder = post(_choice_response("fear_separation"))

    laya.decide(_request(emotions={"fear_separation": definition}), {"base_url": "https://x"})

    assert recorder.body["questions"]["emotion"]["criteria"] == {"fear_separation": "fear separation"}


def test_model_option_is_forwarded_only_when_set(post):
    recorder = post(_choice_response("affectionate"))

    laya.decide(_request(), {"base_url": "https://x", "model": "english"})

    assert recorder.body["model"] == "english"


def test_api_key_env_adds_bearer_header(post, monkeypatch):
    monkeypatch.setenv("LAYA_TOKEN", "sekret")
    recorder = post(_choice_response("affectionate"))

    laya.decide(_request(), {"base_url": "https://x", "api_key_env": "LAYA_TOKEN"})

    assert recorder.calls[0][1]["headers"] == {"Authorization": "Bearer sekret"}


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_api_key_env_is_rejected_without_calling(post, monkeypatch, value):
    monkeypatch.delenv("LAYA_TOKEN", raising=False)
    if value is not None:
        monkeypatch.setenv("LAYA_TOKEN", value)
    recorder = post(_choice_response("affectionate"))

    with pytest.raises(RuntimeError, match="LAYA_TOKEN"):
        laya.decide(_request(), {"base_url": "https://x", "api_key_env": "LAYA_TOKEN"})

    assert recorder.calls == []


@pytest.mark.parametrize("value", [0, -1, 0.0, float("inf"), float("nan"), "10", True, None])
def test_invalid_timeout_is_rejected_without_calling(post, value):
    recorder = post(_choice_response("affectionate"))

    with pytest.raises(ValueError, match="timeout_seconds"):
        laya.decide(_request(), {"base_url": "https://x", "timeout_seconds": value})

    assert recorder.calls == []


def test_custom_timeout_is_passed(post):
    recorder = post(_choice_response("affectionate"))

    laya.decide(_request(), {"base_url": "https://x", "timeout_seconds": 2.5})

    assert recorder.calls[0][1]["timeout"] == 2.5


@pytest.mark.parametrize("options", [{}, {"base_url": ""}, {"base_url": "   "}, {"base_url": 5}])
def test_missing_or_invalid_base_url_is_rejected_without_calling(post, options):
    recorder = post(_choice_response("affectionate"))

    with pytest.raises(ValueError, match="base_url"):
        laya.decide(_request(), options)

    assert recorder.calls == []


def test_options_must_be_a_mapping():
    with pytest.raises(TypeError):
        laya.decide(_request(), None)


def test_empty_emotions_abstains_without_network(post):
    recorder = post(_choice_response("affectionate"))

    assert laya.decide(_request(emotions={}), {"base_url": "https://x"}) is None
    assert recorder.calls == []


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (_response(content=b"<html>", headers={"content-type": "text/html"}), "JSON"),
        (_response(json=["nope"]), "JSON object"),
        (_response(json={"model": "x"}), "answers"),
        (_response(json={"answers": []}), "answers"),
        (_response(json={"answers": {"emotion": "affectionate"}}), "answers.emotion"),
        (_response(json={"answers": {"emotion": {"type": "score", "score": 1.0}}}), "choice"),
        (_response(json={"answers": {"emotion": {"type": "choice"}}}), "allowed emotions"),
        (_response(json={"answers": {"emotion": {"type": "choice", "choice": 3}}}), "allowed emotions"),
        (
            _response(json={"answers": {"emotion": {"type": "choice", "choice": "angry"}}}),
            "allowed emotions",
        ),
    ],
)
def test_malformed_or_invalid_response_raises(post, response, match):
    recorder = post(response)

    with pytest.raises(laya.LayaModError, match=match):
        laya.decide(_request(), {"base_url": "https://x"})

    assert len(recorder.calls) == 1


def test_http_error_status_raises(post):
    recorder = post(_response(500, text="boom"))

    with pytest.raises(laya.LayaModError, match="HTTP 500"):
        laya.decide(_request(), {"base_url": "https://x"})

    assert len(recorder.calls) == 1


def test_network_error_raises(post):
    recorder = post(error=httpx.ConnectError("connection refused"))

    with pytest.raises(laya.LayaModError, match="failed"):
        laya.decide(_request(), {"base_url": "https://x"})

    assert len(recorder.calls) == 1


def test_does_not_mutate_request_or_state(post):
    recorder = post(_choice_response("affectionate"))
    emotions = {"affectionate": {"description": "warm"}, "contentment": {}}
    state = {"longing": 0.5, "nested": {"a": 1}}
    request = _request(emotions=emotions, state=state)
    emotions_snapshot = copy.deepcopy(emotions)
    state_snapshot = copy.deepcopy(state)

    laya.decide(request, {"base_url": "https://x", "model": "english"})

    assert request.emotions == emotions_snapshot
    assert request.state == state_snapshot
    assert state == state_snapshot
    assert emotions == emotions_snapshot
    assert recorder.body["state"]["emotion_state"] == state_snapshot
