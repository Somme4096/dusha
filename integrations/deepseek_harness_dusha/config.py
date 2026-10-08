"""Configuration for the DeepSeek harness adapter."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8765"
DEFAULT_HARNESS = "deepseek-harness"
DEFAULT_ROUTE = "deepseek-harness"
DEFAULT_TIMEOUT_SECONDS = 60.0


@dataclass
class DeepSeekHarnessConfig:
    gateway_url: str = DEFAULT_GATEWAY_URL
    api_token: str = ""
    harness: str = DEFAULT_HARNESS
    route: str = DEFAULT_ROUTE
    request_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    auto_inject: bool = True

    def __post_init__(self) -> None:
        self.gateway_url = self.gateway_url.rstrip("/") or DEFAULT_GATEWAY_URL
        self.request_timeout_seconds = max(1.0, float(self.request_timeout_seconds))
