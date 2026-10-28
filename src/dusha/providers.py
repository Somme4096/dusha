from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .homes import COMPANION_NAME

PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
PROTOCOLS = frozenset({"chat", "messages", "responses"})
AUTH_STYLES = frozenset({"bearer", "x-api-key"})
RESERVED = frozenset({"health"})


def http_url(value: Any, label: str) -> str:
    parts = urlsplit(value) if isinstance(value, str) else None
    plain = parts and parts.scheme in {"http", "https"} and parts.hostname
    if not plain or parts.query or parts.fragment:
        raise ValueError(f"{label} must be an http or https URL without a query, got {value!r}")
    if "@" in parts.netloc:
        raise ValueError(f"{label} must not carry credentials")
    return value.rstrip("/")


def render(value: str, conversation_id: str = "") -> str:
    def fill(match: Any) -> str:
        token = match[1]
        if token == "conversation_id":
            return conversation_id
        variable = token.removeprefix("env:")
        if variable not in os.environ:
            raise ValueError(f"the environment variable {variable} is not set")
        return os.environ[variable]

    return PLACEHOLDER.sub(fill, value)


@dataclass(frozen=True, slots=True)
class Provider:
    base_url: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 120.0
    endpoints: dict[str, str] = field(default_factory=dict)
    key: str = ""
    auth: str = "bearer"

    def url(self, kind: str | None = None) -> str:
        return self.endpoints.get(kind, self.base_url) if kind else self.base_url

    def request_headers(self, conversation_id: str = "") -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.key and self.auth == "x-api-key":
            headers["x-api-key"] = render(self.key, conversation_id)
        elif self.key:
            headers["authorization"] = f"Bearer {render(self.key, conversation_id)}"
        headers.update({name: render(value, conversation_id) for name, value in self.headers.items()})
        return headers


class Providers:
    def __init__(self, providers: dict[str, Provider]):
        self._providers = dict(providers)

    def names(self) -> list[str]:
        return sorted(self._providers)

    def get(self, name: str) -> Provider:
        if name not in self._providers:
            raise KeyError(f"no provider named {name!r}, have {self.names()}")
        return self._providers[name]


def _object(value: Any, label: str, allowed: set[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown field(s) under {label}: {unknown}")
    return value


def _string(raw: dict[str, Any], key: str, label: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{label} must be str")
    return value


def _placeholders(value: str, label: str, *, conversation: bool, environ: bool) -> None:
    for token in PLACEHOLDER.findall(value):
        variable = token.removeprefix("env:")
        if conversation and token == "conversation_id":
            continue
        if token == variable or not variable:
            raise ValueError(f"{label} has an unknown placeholder {{{token}}}")
        if environ and variable not in os.environ:
            raise ValueError(f"{label} needs the environment variable {variable}")


def _timeout(raw: dict[str, Any], label: str) -> float:
    value = raw.get("timeout_seconds", 120.0)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label}.timeout_seconds must be float")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label}.timeout_seconds must be above 0")
    return float(value)


def provider(name: str, raw: Any, *, environ: bool = True) -> Provider:
    label = f"providers.{name}"
    if not COMPANION_NAME.fullmatch(name) or name in RESERVED:
        raise ValueError(
            f"provider name must be letters, digits, dot, dash, or underscore and not one of "
            f"{sorted(RESERVED)}, got {name!r}"
        )
    allowed = {"base_url", "headers", "timeout_seconds", "endpoints", "key", "auth"}
    profile = _object(raw, label, allowed)
    headers = profile.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError(f"{label}.headers must be an object")
    for header, value in headers.items():
        if not isinstance(value, str):
            raise ValueError(f"{label}.headers.{header} must be str")
        _placeholders(value, f"{label}.headers.{header}", conversation=True, environ=environ)
    endpoints = _object(profile.get("endpoints"), f"{label}.endpoints", set(PROTOCOLS))
    key = _string(profile, "key", f"{label}.key", "")
    _placeholders(key, f"{label}.key", conversation=False, environ=environ)
    auth = _string(profile, "auth", f"{label}.auth", "bearer")
    if auth not in AUTH_STYLES:
        raise ValueError(f"{label}.auth must be one of {sorted(AUTH_STYLES)}, got {auth!r}")
    return Provider(
        base_url=http_url(profile.get("base_url", ""), f"{label}.base_url"),
        headers=dict(headers),
        timeout_seconds=_timeout(profile, label),
        endpoints={kind: http_url(url, f"{label}.endpoints.{kind}") for kind, url in endpoints.items()},
        key=key,
        auth=auth,
    )


def parse(raw: Any, *, environ: bool = True) -> dict[str, Provider]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("providers must be an object")
    return {str(name): provider(str(name), value, environ=environ) for name, value in raw.items()}
