"""Small semantic appraisal layer; no vector store or additional model runtime."""

from __future__ import annotations

import logging
import math
import threading
import time

import httpx

from .embedding import MIN_SIMILARITY, OpenAIEmbeddingClient

# Full utterances represent meanings, rather than substring triggers.
PROTOTYPES = {
    "affectionate": "I love you and cherish spending time with you.",
    "playful": "I'm teasing you playfully, let's laugh and have fun together.",
    "vulnerable": "I feel insecure and lonely and need your emotional support.",
    "reassuring": "You can trust me. I am here for you and everything will be okay.",
    "cold": "Leave me alone. I don't want to talk to you.",
    "conflict": "We are arguing because you hurt my feelings.",
    "distant": "I am busy and unavailable to talk right now.",
    "struggling": "I am overwhelmed and exhausted and cannot cope with everything.",
    "intimate_reference": "I wish I could hold you close and kiss you.",
    "intimate_event": "We are sharing a tender kiss and holding each other close.",
    "neutral": "I am sharing an ordinary update about my day.",
    "hostile": "I hate you and want to insult and hurt you.",
    "fear_separation": "I fear losing you, being abandoned, or never seeing you again.",
    "fear_death": "My life is in danger and I might die.",
    "fear_concern": "I am worried something has happened to you. Are you safe?",
    "fear_general": "I am frightened and afraid of what might happen.",
}


class SemanticAppraisal:
    def __init__(self, client: OpenAIEmbeddingClient):
        self.client = client
        self._anchors: list[list[float]] = []
        self._retry_at = 0.0
        self._lock = threading.Lock()

    def weights(self, text: str) -> dict[str, float] | None:
        """Return bounded blend weights, or None when the provider is unavailable."""
        with self._lock:
            if time.monotonic() < self._retry_at:
                return None
            try:
                if not self._anchors:
                    anchors = []
                    texts = list(PROTOTYPES.values())
                    size = max(1, self.client.config.batch_size)
                    for start in range(0, len(texts), size):
                        anchors.extend(self.client.embed(texts[start : start + size]))
                    self._anchors = anchors
                query = self.client.embed([text])[0]
                if any(len(anchor) != len(query) for anchor in self._anchors):
                    self._anchors = []
                    raise ValueError("affect embedding dimensions changed")
                scores = {
                    label: max(
                        0.0,
                        min(
                            1.0,
                            (math.fsum(a * b for a, b in zip(query, anchor, strict=True)) - MIN_SIMILARITY)
                            / (1 - MIN_SIMILARITY),
                        ),
                    )
                    for label, anchor in zip(PROTOTYPES, self._anchors, strict=True)
                }
                total = sum(scores.values())
                # Weak matches retain neutral mass; many matches cannot amplify the step.
                weights = {label: score / max(1.0, total) for label, score in scores.items() if score > 0}
                weights["neutral"] = weights.get("neutral", 0.0) + max(0.0, 1 - total)
                return weights
            except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
                self._retry_at = time.monotonic() + max(0, self.client.config.failure_cooldown_seconds)
                logging.getLogger(__name__).warning("Semantic affect unavailable; using phrase matching")
                return None
