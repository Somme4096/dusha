"""Laya decision mod for the companion gateway.

A single-file, externally hosted adapter: it calls a running Laya service
(https://github.com/NandhaKishorM/laya) over its pure HTTP
``POST /v1/systemone`` protocol and maps the selected choice back to an emotion
label. It deliberately has no dependency on ``laya`` or ``torch`` -- the model
runs in its own process and is reached over the network.

The gateway loads this file dynamically from
``~/.config/companion-gateway/mods/`` and calls::

    decide(request: DecisionRequest, options: Mapping[str, Any]) -> DecisionResult | None

Recognised ``options``:

``base_url`` (required)
    Root URL of the Laya server, e.g. ``http://127.0.0.1:8000``. There is no
    default: a real endpoint is never guessed. ``/v1/systemone`` is appended.
``timeout_seconds`` (optional, default ``10``)
    Finite, positive per-request timeout in seconds.
``api_key_env`` (optional)
    Name of an environment variable holding the bearer token. If configured
    but the variable is unset or empty, the call fails instead of silently
    dropping the configured credentials.
``model`` (optional)
    Laya checkpoint name (e.g. ``english``). Omitted by default so the server
    auto-selects; the gateway does not pin a checkpoint.

The adapter never mutates ``request`` or its ``state``. On a provider failure
or an unusable response it raises ``LayaModError`` for the caller to catch.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Any

import httpx
from companion_gateway.decision import DecisionRequest, DecisionResult

__all__ = ["LayaModError", "decide"]

_DEFAULT_TIMEOUT_SECONDS = 10.0
_SYSTEM_ONE_PATH = "/v1/systemone"


class LayaModError(RuntimeError):
    """Raised when the Laya provider fails or answers unusably."""


def _timeout_seconds(value: Any) -> float:
    """Validate a finite, positive timeout, rejecting bools and non-numbers."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("laya mod option 'timeout_seconds' must be a finite positive number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("laya mod option 'timeout_seconds' must be a finite positive number")
    return seconds


def _auth_headers(api_key_env: Any) -> dict[str, str] | None:
    """Build the bearer header, failing loudly when a configured env var is missing."""

    if api_key_env is None:
        return None
    if not isinstance(api_key_env, str) or not api_key_env.strip():
        raise ValueError("laya mod option 'api_key_env' must be a non-empty environment variable name")
    name = api_key_env.strip()
    token = os.environ.get(name)
    if token is None or not token.strip():
        raise RuntimeError(
            f"laya mod option 'api_key_env' is set to {name!r} but that environment variable "
            "is unset or empty"
        )
    return {"Authorization": f"Bearer {token}"}


def _criteria(emotions: Mapping[str, Any]) -> dict[str, str]:
    """Map each allowed emotion to its description, or a human-readable name."""

    criteria: dict[str, str] = {}
    for dimension, definition in emotions.items():
        name = str(dimension)
        description = None
        if isinstance(definition, Mapping):
            raw = definition.get("description")
            if isinstance(raw, str) and raw.strip():
                description = raw.strip()
        criteria[name] = description if description is not None else (name.replace("_", " ") or name)
    return criteria


def _extract_choice(payload: Any, allowed: Mapping[str, Any]) -> str:
    """Pull a valid ``answers.emotion.choice`` out of a Laya response body."""

    if not isinstance(payload, Mapping):
        raise LayaModError("laya response must be a JSON object")
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise LayaModError("laya response is missing an object 'answers'")
    emotion = answers.get("emotion")
    if not isinstance(emotion, Mapping):
        raise LayaModError("laya response is missing an object 'answers.emotion'")
    if emotion.get("type") != "choice":
        raise LayaModError(f"laya answered emotion with type {emotion.get('type')!r}, expected 'choice'")
    choice = emotion.get("choice")
    if not isinstance(choice, str) or choice not in allowed:
        raise LayaModError(
            f"laya chose emotion {choice!r}, which is not one of the allowed emotions "
            f"{sorted(str(name) for name in allowed)}"
        )
    return choice


def decide(request: DecisionRequest, options: Mapping[str, Any]) -> DecisionResult | None:
    """Ask a Laya server to pick one of ``request.emotions``.

    Returns the selected ``DecisionResult``, or ``None`` when there are no
    allowed emotions to choose between. Raises ``ValueError`` for a bad
    configuration and ``LayaModError`` for a provider or response failure.
    """

    if not isinstance(options, Mapping):
        raise TypeError("laya mod options must be a mapping")

    base_url = options.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("laya mod requires a non-empty string option 'base_url'")
    timeout = _timeout_seconds(options.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
    headers = _auth_headers(options.get("api_key_env"))
    model = options.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("laya mod option 'model' must be a non-empty string when set")

    emotions = request.emotions
    if not emotions:
        return None

    body: dict[str, Any] = {
        "state": {
            "message": request.message,
            "emotion_state": request.state,
        },
        "questions": {
            "emotion": {
                "type": "choice",
                "instructions": request.instruction,
                "criteria": _criteria(emotions),
            }
        },
    }
    if model is not None:
        body["model"] = model.strip()

    url = base_url.strip().rstrip("/") + _SYSTEM_ONE_PATH
    try:
        response = httpx.post(url, json=body, headers=headers, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise LayaModError(f"laya provider returned HTTP {exc.response.status_code} for {url}") from exc
    except httpx.RequestError as exc:
        raise LayaModError(f"laya provider request to {url} failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise LayaModError("laya provider returned a non-JSON response") from exc

    return DecisionResult(emotion=_extract_choice(payload, emotions))
