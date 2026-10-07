"""Small semantic appraisal layer; no vector store or additional model runtime."""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

import httpx

from . import emotions as _emotions
from .embedding import OpenAIEmbeddingClient


class SemanticAppraisal:
    def __init__(self, client: OpenAIEmbeddingClient, spec: dict[str, Any] | None = None):
        self.client = client
        self._anchors: list[list[float]] = []
        self._retry_at = 0.0
        self._lock = threading.Lock()
        self.configure(spec if spec is not None else _emotions.default_emotions()["appraisal"])

    def configure(self, spec: dict[str, Any]) -> None:
        with self._lock:
            self.prototypes = {label: item["text"] for label, item in spec["prototypes"].items()}
            self.min_similarity = float(spec["min_similarity"])
            self.fallback_label = str(spec["fallback_label"])
            self._anchors = []

    def weights(self, text: str) -> dict[str, float] | None:
        """Return bounded blend weights, or None when the provider is unavailable."""
        with self._lock:
            if not self.prototypes or time.monotonic() < self._retry_at:
                return None
            try:
                if not self._anchors:
                    anchors = []
                    texts = list(self.prototypes.values())
                    size = max(1, self.client.config.batch_size)
                    for start in range(0, len(texts), size):
                        anchors.extend(self.client.embed(texts[start : start + size]))
                    self._anchors = anchors
                query = self.client.embed([text])[0]
                floor = self.min_similarity
                if any(len(anchor) != len(query) for anchor in self._anchors):
                    self._anchors = []
                    raise ValueError("affect embedding dimensions changed")
                scores = {
                    label: max(
                        0.0,
                        min(
                            1.0,
                            (math.fsum(a * b for a, b in zip(query, anchor, strict=True)) - floor)
                            / (1 - floor),
                        ),
                    )
                    for label, anchor in zip(self.prototypes, self._anchors, strict=True)
                }
                total = sum(scores.values())
                # Weak matches leave their remaining mass to the fallback label.
                weights = {label: score / max(1.0, total) for label, score in scores.items() if score > 0}
                if self.fallback_label:
                    fallback = self.fallback_label
                    weights[fallback] = weights.get(fallback, 0.0) + max(0.0, 1 - total)
                return weights
            except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
                self._retry_at = time.monotonic() + max(0, self.client.config.failure_cooldown_seconds)
                logging.getLogger(__name__).warning("Semantic affect unavailable; using phrase matching")
                return None
