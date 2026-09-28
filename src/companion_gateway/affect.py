from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import emotions as _emotions
from . import prompts as _prompts
from .affect_semantic import SemanticAppraisal
from .config import AffectConfig
from .database import Database
from .serialization import compact_json
from .timeutil import isoformat, parse_time, utc_now

Decider = Callable[..., "str | None"]
PhraseMatcher = Callable[[str], dict[str, float] | None]

SEMANTIC_LABEL_DIMENSIONS: dict[str, dict[str, float]] = {
    "affectionate": {
        "intimacy": 0.20,
        "contentment": 0.15,
        "anxiety": -0.18,
        "lust": 0.12,
        "longing": -0.10,
        "fear": -0.08,
    },
    "playful": {
        "play": 0.20,
        "elation": 0.18,
        "contentment": 0.12,
        "seeking": 0.10,
        "irritability": -0.10,
        "lust": 0.10,
    },
    "vulnerable": {"intimacy": 0.25, "protectiveness": 0.20, "anxiety": 0.12, "dejection": 0.08},
    "reassuring": {
        "anxiety": -0.25,
        "jealousy": -0.20,
        "contentment": 0.15,
        "intimacy": 0.15,
        "fear": -0.15,
    },
    "cold": {"anxiety": 0.15, "dejection": 0.12, "longing": 0.10, "intimacy": -0.10},
    "conflict": {
        "anxiety": 0.20,
        "irritability": 0.15,
        "dejection": 0.15,
        "possessiveness": 0.18,
        "intimacy": -0.15,
        "contentment": -0.15,
    },
    "distant": {"anxiety": 0.12, "dejection": 0.10, "longing": 0.12, "intimacy": -0.08},
    "struggling": {
        "protectiveness": 0.30,
        "anxiety": 0.12,
        "dejection": 0.12,
        "contentment": -0.08,
        "fatigue": 0.12,
    },
    "intimate_reference": {"lust": 0.18, "intimacy": 0.10},
    "intimate_event": {"lust": 0.25, "intimacy": 0.18},
    "neutral": {"anxiety": -0.05, "longing": -0.04, "contentment": 0.03},
    "hostile": {
        "dejection": 0.22,
        "anxiety": 0.18,
        "irritability": 0.12,
        "intimacy": -0.22,
        "contentment": -0.18,
    },
    "fear_separation": {
        "fear": 0.20,
        "longing": 0.15,
        "possessiveness": 0.12,
        "anxiety": 0.15,
        "protectiveness": 0.10,
        "dejection": 0.10,
        "irritability": 0.08,
    },
    "fear_death": {
        "fear": 0.35,
        "anxiety": 0.30,
        "irritability": 0.20,
        "contentment": -0.12,
        "play": -0.15,
        "elation": -0.10,
    },
    "fear_concern": {
        "fear": 0.28,
        "longing": 0.12,
        "possessiveness": 0.15,
        "anxiety": 0.20,
        "protectiveness": 0.25,
        "contentment": -0.10,
    },
    "fear_general": {"fear": 0.20, "anxiety": 0.10},
}


class AffectEngine:
    def __init__(
        self,
        database: Database,
        config: AffectConfig,
        prompts: dict[str, Any] | None = None,
        decision_increment: float = 0.1,
        appraisal: SemanticAppraisal | None = None,
    ):
        self.database = database
        self.config = config
        self.appraisal = appraisal
        self._lock = threading.RLock()
        self.emotions = _emotions.resolve_emotions(config)
        self.emotions_version = str(self.emotions["emotion_version"])
        self.emotions_fingerprint = _emotions.fingerprint(self.emotions)
        self.spec = {name: values.copy() for name, values in self.emotions["dimensions"].items()}
        value_range = self.emotions["value_range"]
        self.value_min = float(value_range["min"])
        self.value_max = float(value_range["max"])
        self.decision_increment = float(decision_increment)
        self.affect_presentation = dict(
            (prompts or _prompts.default_prompts())["affect_presentation"]
        )

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
        return row, json.loads(row["state_json"])

    def _clamp(self, value: float, floor: float = 0.0) -> float:
        return min(self.value_max, max(floor, value))

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
            effective = (
                delta * impact * (1 - current) if delta > 0 else delta * impact * current
            )
            next_value = max(current + effective, self.spec[name]["floor"])
            if delta < 0 and name in negative:
                next_value = max(next_value, min(current, float(mood[name])))
            base[name] = self._clamp(next_value, self.spec[name]["floor"])

    def _increase(self, state: dict[str, Any], emotion: str) -> None:
        spec = self.spec[emotion]
        current = float(state["base"][emotion])
        state["base"][emotion] = self._clamp(
            current + self.decision_increment, spec["floor"]
        )

    def _semantic_emotion(self, message: str) -> str | None:
        try:
            weights = self.appraisal.weights(message)
        except Exception:
            return None
        if not isinstance(weights, dict) or not weights:
            return None
        blended: dict[str, float] = {}
        for label, weight in weights.items():
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                continue
            for dimension, delta in SEMANTIC_LABEL_DIMENSIONS.get(label, {}).items():
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
                gain_cfg["min"], min(gain_cfg["max"], gain_cfg["factor"] * deviation)
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
            caps = self.emotions["silence"]["caps"]
            affect = self.emotions["affect"]
            rates = {
                "longing": affect["silence_longing_per_hour"],
                "anxiety": affect["silence_anxiety_per_hour"],
                "seeking": affect["silence_seeking_per_hour"],
            }
            for name, rate in rates.items():
                neutral = self.spec[name]["neutral"]
                state["base"][name] = min(neutral + caps[name], state["base"][name] + rate * silence_hours)
            total_silence = (now - last_user).total_seconds() / 3600
            if total_silence >= self.emotions["silence"]["dejection_gate_hours"]:
                state["base"]["dejection"] = min(
                    self.spec["dejection"]["neutral"] + caps["dejection"],
                    state["base"]["dejection"]
                    + self.emotions["silence"]["dejection_rate_per_hour"] * silence_hours,
                )

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
        decider: Decider | None,
        instruction: str,
        phrase_matcher: PhraseMatcher | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        requested = now if now is not None else utc_now()
        with self._lock, self.database.connect() as db:
            row, state = self._load_row(db, requested)
            current = max(requested, parse_time(row["last_updated_at"]))
            self._advance(row, state, current)
            db.execute(
                "UPDATE proactive_events SET status='cancelled', updated_at=? "
                "WHERE status IN ('pending','leased')",
                (isoformat(current),),
            )
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

        emotion = None
        if decider is not None:
            try:
                emotion = decider(
                    message=message,
                    emotions=self.spec,
                    state=snapshot,
                    instruction=instruction,
                )
            except Exception:
                emotion = None
            if not isinstance(emotion, str) or emotion not in self.spec:
                emotion = None

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
                    (source_message_id, emotion, self.decision_increment, isoformat(current)),
                )
                if cursor.lastrowid is not None:
                    decision_id = int(cursor.lastrowid)
                updated = db.execute("SELECT * FROM affect_state WHERE id=1").fetchone()
                public = self._public_state(state, updated, current)

        if phrase_matcher is not None:
            try:
                deltas = phrase_matcher(message)
                if isinstance(deltas, dict):
                    deltas = dict(deltas.items())
                else:
                    deltas = None
            except Exception:
                deltas = None
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
            "increment": self.decision_increment,
            "decision_id": decision_id,
            "state": public,
        }

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
        if values["fear"] >= prompt["fear_minimum"] and not any(
            name == "fear" for name, _ in selected
        ):
            selected.append(("fear", values["fear"]))
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
