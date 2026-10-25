from __future__ import annotations

import copy
import json
import logging
import math
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter

from . import emotions as _emotions
from . import schema as _schema
from .affect_semantic import SemanticAppraisal
from .config import AffectConfig, EmbeddingConfig
from .database import Database
from .embedding import OpenAIEmbeddingClient
from .hooks import Block, ContextRequest, DecisionRequest
from .interfaces import call, errors
from .serialization import compact_json
from .timeutil import isoformat, parse_time, utc_now

logger = logging.getLogger("dusha")

Decide = Callable[[DecisionRequest], list[dict[str, Any]]]

name = "affect"
inject = ("database", "settings", "prompts", "api", "cli")

SCHEMA = """
CREATE TABLE IF NOT EXISTS affect_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    state_json TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    last_user_message_at TEXT,
    last_interaction_at TEXT,
    last_proactive_sent_at TEXT,
    unanswered_proactive INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS affect_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    emotion TEXT NOT NULL,
    increment REAL NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS affect_decisions_message
ON affect_decisions(source_message_id, id);
"""


class AffectEngine:
    name = name

    def __init__(
        self,
        database: Database,
        config: AffectConfig,
        embedding: EmbeddingConfig,
        prompts: dict[str, Any],
        proposals: Decide | None = None,
    ):
        self.database = database
        database.migrate(SCHEMA)
        self.config = config
        self.prompts = prompts
        self.proposals = proposals or (lambda request: [])
        self.appraisal = (
            SemanticAppraisal(OpenAIEmbeddingClient(embedding))
            if embedding.base_url and embedding.model
            else None
        )
        self._lock = threading.RLock()
        self.emotions = _emotions.resolve_emotions(config, None, config.increment)
        self.emotions_version = str(self.emotions["emotion_version"])
        self.spec = {name: values.copy() for name, values in self.emotions["dimensions"].items()}
        value_range = self.emotions["value_range"]
        self.value_min = float(value_range["min"])
        self.value_max = float(value_range["max"])
        self.value_span = self.value_max - self.value_min
        self.affect_presentation = dict(prompts["affect_presentation"])
        if self.appraisal is not None:
            self.appraisal.configure(self.emotions["appraisal"])

    def initial_state(self) -> dict[str, Any]:
        base = {name: values["neutral"] for name, values in self.spec.items()}
        return {"base": base.copy(), "mood": base.copy()}

    def _ensure(self, db: Any, now: datetime) -> None:
        state = self.initial_state()
        db.execute(
            """INSERT OR IGNORE INTO affect_state
               (id, state_json, last_updated_at, last_interaction_at)
               VALUES(1,?,?,?)""",
            (compact_json(state, ensure_ascii=True), isoformat(now), isoformat(now)),
        )

    def _load_row(self, db: Any, now: datetime) -> tuple[Any, dict[str, Any]]:
        self._ensure(db, now)
        row = db.execute("SELECT * FROM affect_state WHERE id=1").fetchone()
        stored = json.loads(row["state_json"])
        neutral = self.initial_state()
        return row, {
            layer: {name: stored.get(layer, {}).get(name, default) for name, default in defaults.items()}
            for layer, defaults in neutral.items()
        }

    def _clamp(self, value: float, floor: float | None = None) -> float:
        return min(self.value_max, max(self.value_min if floor is None else floor, value))

    def increment_for(self, emotion: str) -> float:
        return float(self.spec[emotion].get("increment", self.emotions["decision"]["increment"]))

    def _apply_deltas(self, state: dict[str, Any], deltas: dict[str, float], scale: float = 1.0) -> None:
        base = state["base"]
        mood = state["mood"]
        negative = set(self.emotions["negative_dimensions"])
        impact = float(self.emotions["impact_scale"])
        for name, nominal in deltas.items():
            if not isinstance(name, str) or name not in base or isinstance(nominal, bool):
                continue
            try:
                delta = float(nominal) * scale
            except (TypeError, ValueError):
                continue
            if not math.isfinite(delta):
                continue
            current = float(base[name])
            room = self.value_max - current if delta > 0 else current - self.value_min
            effective = delta * impact * room / self.value_span
            next_value = max(current + effective, self.spec[name]["floor"])
            if delta < 0 and name in negative:
                next_value = max(next_value, min(current, float(mood[name])))
            base[name] = self._clamp(next_value, self.spec[name]["floor"])

    def _increase(self, state: dict[str, Any], emotion: str) -> None:
        spec = self.spec[emotion]
        current = float(state["base"][emotion])
        state["base"][emotion] = self._clamp(
            current + self.increment_for(emotion), spec["floor"]
        )

    def _semantic_emotion(self, message: str) -> str | None:
        try:
            weights = self.appraisal.weights(message)
        except Exception:
            return None
        if not isinstance(weights, dict) or not weights:
            return None
        prototypes = self.emotions["appraisal"]["prototypes"]
        blended: dict[str, float] = {}
        for label, weight in weights.items():
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                continue
            for dimension, delta in prototypes.get(label, {}).get("deltas", {}).items():
                blended[dimension] = blended.get(dimension, 0.0) + float(weight) * delta
        positive = {name: value for name, value in blended.items() if value > 0 and name in self.spec}
        if not positive:
            return None
        return max(positive, key=positive.get)

    def _advance_values(self, state: dict[str, Any], hours: float) -> None:
        if hours <= 0:
            return
        gain_cfg = self.emotions["mood_follow_gain"]
        mood_follow_hours = self.emotions["affect"]["mood_follow_hours"]
        mood_return_hours = self.emotions["affect"]["mood_return_hours"]
        for name, params in self.spec.items():
            base = float(state["base"].get(name, params["neutral"]))
            mood = float(state["mood"].get(name, params["neutral"]))
            deviation = abs(base - mood)
            gain = max(
                gain_cfg["min"], min(gain_cfg["max"], gain_cfg["factor"] * deviation / self.value_span)
            )
            follow = 1 - math.exp(-hours * gain / mood_follow_hours)
            mood += (base - mood) * follow
            mood = params["neutral"] + (mood - params["neutral"]) * math.exp(
                -hours / mood_return_hours
            )
            floor = params["floor"]
            state["mood"][name] = self._clamp(mood, floor)
            state["base"][name] = self._clamp(
                state["mood"][name] + (base - state["mood"][name]) * math.exp(-hours / params["tau"]),
                floor,
            )

    def _advance(self, row: Any, state: dict[str, Any], now: datetime) -> None:
        last_updated = parse_time(row["last_updated_at"])
        hours = max(0.0, (now - last_updated).total_seconds() / 3600)
        self._advance_values(state, hours)
        last_user = parse_time(row["last_user_message_at"]) if row["last_user_message_at"] else None
        if last_user and now > last_updated:
            silence_start = max(last_updated, last_user)
            silence_hours = max(0.0, (now - silence_start).total_seconds() / 3600)
            total_silence = max(0.0, (now - last_user).total_seconds() / 3600)
            for name, rule in self.emotions["silence"]["rules"].items():
                if total_silence < rule["gate_hours"]:
                    continue
                current = float(state["base"][name])
                drifted = current + rule["rate_per_hour"] * silence_hours
                state["base"][name] = max(current, min(self.spec[name]["neutral"] + rule["cap"], drifted))

    def _save(self, db: Any, state: dict[str, Any], now: datetime, **fields: Any) -> None:
        assignments = ["state_json=?", "last_updated_at=?", "revision=revision+1"]
        values: list[Any] = [compact_json(state, ensure_ascii=True), isoformat(now)]
        for name, value in fields.items():
            assignments.append(f"{name}=?")
            values.append(value)
        db.execute(f"UPDATE affect_state SET {', '.join(assignments)} WHERE id=1", values)

    def record_user_message(
        self,
        *,
        message: str,
        source_message_id: int | None,
        decide: Decide | None = None,
        instruction: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        if instruction is None:
            instruction = str(self.prompts["decision_instruction"])
        requested = now if now is not None else utc_now()
        with self._lock, self.database.connect() as db:
            row, state = self._load_row(db, requested)
            current = max(requested, parse_time(row["last_updated_at"]))
            self._advance(row, state, current)
            self._save(db, state, current, last_interaction_at=isoformat(current))
            db.execute(
                """UPDATE affect_state SET
                   last_user_message_at=CASE
                     WHEN last_user_message_at IS NULL OR last_user_message_at < ? THEN ?
                     ELSE last_user_message_at END,
                   unanswered_proactive=0
                   WHERE id=1""",
                (isoformat(current), isoformat(current)),
            )

            updated = db.execute("SELECT * FROM affect_state WHERE id=1").fetchone()
            snapshot = self._public_state(state, updated, current)

        request = DecisionRequest(str(message), copy.deepcopy(self.spec), snapshot, instruction)
        emotion: str | None = None
        deltas: dict[str, Any] = {}
        for proposal in (decide or self.proposals)(request):
            choice = proposal.get("emotion")
            if emotion is None and choice is not None:
                if isinstance(choice, str) and choice in self.spec:
                    emotion = choice
                else:
                    logger.warning("decision returned invalid emotion: %r", choice)
            if isinstance(proposal.get("deltas"), dict):
                deltas.update(proposal["deltas"])

        if emotion is None and self.appraisal is not None:
            emotion = self._semantic_emotion(message)

        decision_id = None
        public = snapshot
        if emotion is not None:
            with self._lock, self.database.connect() as db:
                row, state = self._load_row(db, requested)
                phase_now = now if now is not None else utc_now()
                current = max(phase_now, parse_time(row["last_updated_at"]))
                self._advance(row, state, current)
                self._increase(state, emotion)
                self._save(db, state, current)
                cursor = db.execute(
                    """INSERT INTO affect_decisions
                       (source_message_id, emotion, increment, occurred_at)
                       VALUES(?,?,?,?)""",
                    (source_message_id, emotion, self.increment_for(emotion), isoformat(current)),
                )
                if cursor.lastrowid is not None:
                    decision_id = int(cursor.lastrowid)
                updated = db.execute("SELECT * FROM affect_state WHERE id=1").fetchone()
                public = self._public_state(state, updated, current)

        unknown = sorted(str(name) for name in deltas if name not in self.spec)
        if unknown:
            logger.warning("decision deltas name unknown emotion dimension(s): %s", unknown)
        if deltas:
            with self._lock, self.database.connect() as db:
                row, state = self._load_row(db, requested)
                phase_now = now if now is not None else utc_now()
                current = max(phase_now, parse_time(row["last_updated_at"]))
                self._advance(row, state, current)
                self._apply_deltas(state, deltas)
                self._save(db, state, current)
                updated = db.execute("SELECT * FROM affect_state WHERE id=1").fetchone()
                public = self._public_state(state, updated, current)
        if emotion is None:
            return None
        return {
            "emotion": emotion,
            "increment": self.increment_for(emotion),
            "decision_id": decision_id,
            "state": public,
        }

    def on_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if message.get("role") != "user":
            return None
        return self.record_user_message(
            message=message["text"],
            source_message_id=message["id"],
            now=parse_time(message["occurred_at"]),
        )

    def on_context(self, request: ContextRequest) -> Block:
        snapshot = self.status()
        slot = self.prompts["affect"]
        description = self.describe(snapshot)
        data = {
            "values": {"base": snapshot["base"], "mood": snapshot["mood"]},
            "description": description,
            "preface": slot["instruction"],
            "versions": {"emotions": self.emotions_version, "prompts": str(self.prompts["prompts_version"])},
        }
        text = slot["open_delimiter"] + slot["instruction"] + "\n" + description + slot["close_delimiter"]
        return Block(self.name, text, data)

    def close(self) -> None:
        if self.appraisal is not None:
            self.appraisal.client.close()

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or utc_now()
        with self._lock, self.database.connect() as db:
            row, state = self._load_row(db, current)
            self._advance(row, state, current)
            self._save(db, state, current)
            updated = db.execute("SELECT * FROM affect_state WHERE id=1").fetchone()
        return self._public_state(state, updated, current)

    def on_proactive_sent(self, now: datetime | None = None) -> None:
        current = now or utc_now()
        with self._lock, self.database.connect() as db:
            row, state = self._load_row(db, current)
            self._advance(row, state, current)
            self._apply_deltas(state, self.emotions["proactive_sent_deltas"])
            self._save(
                db,
                state,
                current,
                last_proactive_sent_at=isoformat(current),
                unanswered_proactive=int(row["unanswered_proactive"]) + 1,
            )

    def describe(self, snapshot: dict[str, Any]) -> str:
        values = snapshot["base"]
        prompt = self.emotions["prompt"]
        presentation = self.affect_presentation
        deviations = sorted(
            ((abs(values[name] - self.spec[name]["neutral"]), name, values[name]) for name in values),
            reverse=True,
        )
        selected = [
            (name, value)
            for deviation, name, value in deviations
            if deviation >= prompt["deviation_threshold"]
        ][: prompt["top_n"]]
        for name, minimum in prompt["always_show"].items():
            if values[name] >= minimum and not any(shown == name for shown, _ in selected):
                selected.append((name, values[name]))
        if not selected:
            return presentation["baseline"]
        labels = []
        for name, value in selected:
            level = (
                presentation["level_high"]
                if value >= prompt["level_high"]
                else presentation["level_elevated"]
                if value >= prompt["level_elevated"]
                else presentation["level_noticeable"]
            )
            labels.append(f"{name}{presentation['level_connector']}{level}")
        return (
            presentation["prefix"]
            + presentation["separator"].join(labels)
            + presentation["suffix"]
        )

    def prompt_context(self, now: datetime | None = None) -> str:
        return self.describe(self.status(now))

    def _public_state(self, state: dict[str, Any], row: Any, now: datetime) -> dict[str, Any]:
        return {
            "base": {key: round(float(value), 4) for key, value in state["base"].items()},
            "mood": {key: round(float(value), 4) for key, value in state["mood"].items()},
            "last_updated_at": isoformat(now),
            "last_user_message_at": row["last_user_message_at"] if row else None,
            "last_proactive_sent_at": row["last_proactive_sent_at"] if row else None,
            "unanswered_proactive": int(row["unanswered_proactive"]) if row else 0,
        }


def apply(ctx: Any, config: AffectConfig) -> None:
    engine = AffectEngine(
        ctx.database,
        config,
        ctx.settings.embedding,
        ctx.prompts,
        lambda request: list(ctx.collect("decide", request).values()),
    )
    ctx.provide("affect", engine)
    ctx.on("message", engine.on_message)
    ctx.on("context", engine.on_context)
    ctx.on_dispose(engine.close)
    _routes(ctx.api, engine)
    ctx.cli.group("affect", "Inspect affect").command(
        "show", engine.status, help="Show the affect state and advance decay"
    )


def _routes(router: APIRouter, engine: AffectEngine) -> None:
    @router.get(
        "/state/v1/affect",
        tags=["state"],
        response_model=_schema.AffectStatusResponse,
        summary="Read affect state (advances decay)",
        responses=errors(503),
    )
    async def affect_status() -> dict[str, Any]:
        return await call(engine.status)
