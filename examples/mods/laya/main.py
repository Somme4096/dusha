from __future__ import annotations

import ipaddress
import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin

import httpx

__all__ = ["LayaModError", "decide"]

_DEFAULT_TIMEOUT_SECONDS = 10.0
_SYSTEM_ONE_PATH = "/v1/systemone"


class LayaModError(RuntimeError):
    """Raised when the Laya provider fails or answers unusably."""


def _timeout_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("laya mod option 'timeout_seconds' must be a finite positive number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("laya mod option 'timeout_seconds' must be a finite positive number")
    return seconds


def _allowed_ips(value: Any) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("laya mod option 'allowed_ips' must be a list of IPv4 or IPv6 addresses")
    addresses = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("laya mod option 'allowed_ips' must contain only IP address strings")
        try:
            addresses.add(ipaddress.ip_address(item))
        except ValueError as exc:
            raise ValueError(f"laya mod option 'allowed_ips' contains invalid address {item!r}") from exc
    return frozenset(addresses)


def _url_for_request(base_url: Any, allowed: frozenset | None) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("laya mod requires a non-empty string option 'base_url'")
    url = base_url.strip().rstrip("/") + _SYSTEM_ONE_PATH
    if allowed is not None:
        parsed = httpx.URL(url)
        try:
            address = ipaddress.ip_address(parsed.host or "")
        except ValueError as exc:
            raise ValueError("laya mod requires a literal IP base_url when 'allowed_ips' is set") from exc
        if address not in allowed:
            raise ValueError(f"laya mod base_url address {address} is not in 'allowed_ips'")
    return url


def _headers(options: Mapping[str, Any]) -> dict[str, str] | None:
    token = options.get("api_key")
    if token is None:
        return None
    if not isinstance(token, str) or not token.strip():
        raise ValueError("laya mod option 'api_key' must be a non-empty string when set")
    return {"Authorization": f"Bearer {token}"}


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = httpx.URL(url)
    return parsed.scheme, parsed.host or "", parsed.port


def _criteria(emotions: Mapping[str, Any]) -> dict[str, str]:
    criteria: dict[str, str] = {}
    for dimension, definition in emotions.items():
        name = str(dimension)
        description = definition.get("description") if isinstance(definition, Mapping) else None
        criteria[name] = description.strip() if isinstance(description, str) and description.strip() else name.replace("_", " ")
    return criteria


def _extract_choice(payload: Any, allowed: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping):
        raise LayaModError("laya response must be a JSON object")
    answer = payload.get("answers")
    emotion = answer.get("emotion") if isinstance(answer, Mapping) else None
    if not isinstance(emotion, Mapping):
        raise LayaModError("laya response is missing an object 'answers.emotion'")
    if emotion.get("type") != "choice":
        raise LayaModError("laya response emotion must have type 'choice'")
    choice = emotion.get("choice")
    if not isinstance(choice, str) or choice not in allowed:
        raise LayaModError(f"laya chose emotion {choice!r}, which is not an allowed emotion")
    return choice


def decide(request: Any, options: Mapping[str, Any]) -> dict[str, str] | None:
    if not isinstance(options, Mapping):
        raise TypeError("laya mod options must be a mapping")
    allowed_ips = _allowed_ips(options.get("allowed_ips"))
    url = _url_for_request(options.get("base_url"), allowed_ips)
    timeout = _timeout_seconds(options.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
    headers = _headers(options)
    model = options.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("laya mod option 'model' must be a non-empty string when set")
    emotions = request.emotions
    if not emotions:
        return None
    body: dict[str, Any] = {
        "state": {"message": request.message, "emotion_state": request.state},
        "questions": {"emotion": {"type": "choice", "instructions": request.instruction, "criteria": _criteria(emotions)}},
    }
    if model is not None:
        body["model"] = model.strip()
    try:
        with httpx.Client(follow_redirects=False, timeout=timeout) as client:
            current = url
            current_headers = headers
            for _ in range(10):
                if allowed_ips is not None:
                    parsed = httpx.URL(current)
                    try:
                        address = ipaddress.ip_address(parsed.host or "")
                    except ValueError as exc:
                        raise LayaModError("laya redirect target must use a literal IP address") from exc
                    if address not in allowed_ips:
                        raise LayaModError(f"laya redirect address {address} is not in 'allowed_ips'")
                response = client.post(current, json=body, headers=current_headers)
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise LayaModError("laya provider returned a redirect without a location")
                    next_url = urljoin(current, location)
                    current_scheme, _, _ = _origin(current)
                    next_scheme, _, _ = _origin(next_url)
                    if current_scheme == "https" and next_scheme != "https":
                        raise LayaModError("laya provider HTTPS redirect downgrade is not allowed")
                    if headers is not None and _origin(current) != _origin(next_url):
                        current_headers = None
                    current = next_url
                    continue
                response.raise_for_status()
                break
            else:
                raise LayaModError("laya provider returned too many redirects")
    except LayaModError:
        raise
    except httpx.HTTPStatusError as exc:
        raise LayaModError(f"laya provider returned HTTP {exc.response.status_code} for {current}") from exc
    except httpx.RequestError as exc:
        raise LayaModError(f"laya provider request to {current} failed: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise LayaModError("laya provider returned a non-JSON response") from exc
    return {"emotion": _extract_choice(payload, emotions)}
