from __future__ import annotations

import math
import os
import threading
from typing import Any

import httpx

from .config import EmbeddingConfig


class OpenAIEmbeddingClient:
    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._get_client().post(
            self.config.base_url.rstrip("/") + "/embeddings",
            headers=self._headers(),
            json=self._payload(texts),
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("embedding endpoint returned an unexpected item count")
        if not all(isinstance(item, dict) for item in data):
            raise ValueError("embedding endpoint returned an invalid data item")
        indices = [int(item.get("index", -1)) for item in data]
        if sorted(indices) != list(range(len(texts))):
            raise ValueError("embedding endpoint returned invalid item indices")
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors = [self._normalize(item.get("embedding")) for item in ordered]
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1:
            raise ValueError("embedding endpoint returned inconsistent dimensions")
        return vectors

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _get_client(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(timeout=self.config.timeout_seconds)
            return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        key = os.getenv(self.config.api_key_env, "") if self.config.api_key_env else ""
        if key:
            headers["authorization"] = f"Bearer {key}"
        return headers

    def _payload(self, texts: list[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "input": texts,
            "encoding_format": "float",
        }
        if self.config.dimensions is not None:
            payload["dimensions"] = self.config.dimensions
        return payload

    @staticmethod
    def _normalize(value: Any) -> list[float]:
        if not isinstance(value, list) or not value:
            raise ValueError("embedding endpoint returned an invalid vector")
        vector = [float(item) for item in value]
        if not all(math.isfinite(item) for item in vector):
            raise ValueError("embedding endpoint returned a non-finite vector")
        norm = math.sqrt(math.fsum(item * item for item in vector))
        if norm == 0:
            raise ValueError("embedding endpoint returned a zero vector")
        return [item / norm for item in vector]
