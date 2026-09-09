from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .affect_semantic import SemanticAppraisal
from .config import DEFAULT_LABEL_PATTERNS, AffectConfig
from .database import Database
from .timeutil import isoformat, parse_time, utc_now

# This deterministic two-timescale model is adapted from Drivesoid v2.0.0.
# Drivesoid v2.0.0 is MIT licensed. See THIRD_PARTY_NOTICES.md.
DIMENSIONS: dict[str, dict[str, float]] = {
    "vitality": {"neutral": 0.50, "floor": 0.08, "tau": 6},
    "fatigue": {"neutral": 0.20, "floor": 0.02, "tau": 6},
    "longing": {"neutral": 0.30, "floor": 0.15, "tau": 6},
    "intimacy": {"neutral": 0.35, "floor": 0.06, "tau": 10},
    "possessiveness": {"neutral": 0.30, "floor": 0.05, "tau": 4},
    "lust": {"neutral": 0.30, "floor": 0.05, "tau": 4},
    "jealousy": {"neutral": 0.22, "floor": 0.00, "tau": 2},
    "anxiety": {"neutral": 0.20, "floor": 0.02, "tau": 5},
    "protectiveness": {"neutral": 0.25, "floor": 0.05, "tau": 4},
    "fear": {"neutral": 0.00, "floor": 0.00, "tau": 7},
    "contentment": {"neutral": 0.35, "floor": 0.06, "tau": 8},
    "elation": {"neutral": 0.20, "floor": 0.02, "tau": 3},
    "seeking": {"neutral": 0.25, "floor": 0.12, "tau": 4},
    "play": {"neutral": 0.25, "floor": 0.03, "tau": 3},
    "dejection": {"neutral": 0.15, "floor": 0.00, "tau": 8},
    "irritability": {"neutral": 0.15, "floor": 0.00, "tau": 3},
}


LABEL_DELTAS: dict[str, dict[str, float]] = {
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
    "reassuring": {"anxiety": -0.25, "jealousy": -0.20, "contentment": 0.15, "intimacy": 0.15, "fear": -0.15},
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

CONTACT_DELTAS = {"longing": -0.06, "seeking": -0.04}
SOOTHING_DELTAS = {"dejection": -0.08, "contentment": 0.03, "anxiety": -0.025, "irritability": -0.02}
NEGATIVE_LABELS = {
    "cold",
    "conflict",
    "distant",
    "hostile",
    "struggling",
    "fear_separation",
    "fear_death",
    "fear_concern",
    "fear_general",
}
SOOTHING_LABELS = {"affectionate", "playful", "reassuring", "neutral"}


@dataclass(slots=True)
class AffectResult:
    label: str
    state: dict[str, Any]
    event_id: int | None


class AffectEngine:
    def __init__(
        self, database: Database, config: AffectConfig, appraisal: SemanticAppraisal | None = None
    ):
        self.database = database
        self.config = config
        self.appraisal = appraisal
        self.label_patterns = dict(DEFAULT_LABEL_PATTERNS)
        self.label_patterns.update(config.label_patterns)
        self.spec = {name: values.copy() for name, values in DIMENSIONS.items()}
        for name, override in config.dimensions.items():
            if name in self.spec:
                self.spec[name].update(
                    {
                        key: float(value)
                        for key, value in override.items()
                        if key in {"neutral", "floor", "tau"}
                    }
                )

    def initial_state(self) -> dict[str, Any]:
        base = {name: values["neutral"] for name, values in self.spec.items()}
        return {"base": base.copy(), "mood": base.copy(), "recent_labels": []}

    def _ensure(self, now: datetime) -> None:
        state = self.initial_state()
        with self.database.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO affect_state
                   (id, state_json, last_updated_at, last_interaction_at)
                   VALUES(1,?,?,?)""",
                (json.dumps(state, separators=(",", ":")), isoformat(now), isoformat(now)),
            )

    def _load_row(self, now: datetime) -> tuple[Any, dict[str, Any]]:
        self._ensure(now)
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM affect_state WHERE id=1").fetchone()
        return row, json.loads(row["state_json"])

    @staticmethod
    def _clamp(value: float, floor: float = 0.0) -> float:
        return min(1.0, max(floor, value))

    def _apply_deltas(self, state: dict[str, Any], deltas: dict[str, float], scale: float = 1.0) -> None:
        base = state["base"]
        mood = state["mood"]
        negative = {"dejection", "irritability", "anxiety", "fear"}
        for name, nominal in deltas.items():
            if name not in base:
                continue
            delta = nominal * scale
            current = float(base[name])
            effective = delta * 2 * (1 - current) if delta > 0 else delta * 2 * current
            next_value = max(current + effective, self.spec[name]["floor"])
            if delta < 0 and name in negative:
                next_value = max(next_value, min(current, float(mood[name])))
            base[name] = self._clamp(next_value, self.spec[name]["floor"])

    def _advance_values(self, state: dict[str, Any], hours: float) -> None:
        if hours <= 0:
            return
        for name, params in self.spec.items():
            base = float(state["base"].get(name, params["neutral"]))
            mood = float(state["mood"].get(name, params["neutral"]))
            deviation = abs(base - mood)
            gain = max(0.25, min(2.5, 4 * deviation))
            follow = 1 - math.exp(-hours * gain / self.config.mood_follow_hours)
            mood += (base - mood) * follow
            mood = params["neutral"] + (mood - params["neutral"]) * math.exp(
                -hours / self.config.mood_return_hours
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
            caps = {"longing": 0.35, "anxiety": 0.18, "seeking": 0.12, "dejection": 0.08}
            rates = {
                "longing": self.config.silence_longing_per_hour,
                "anxiety": self.config.silence_anxiety_per_hour,
                "seeking": self.config.silence_seeking_per_hour,
            }
            for name, rate in rates.items():
                neutral = self.spec[name]["neutral"]
                state["base"][name] = min(neutral + caps[name], state["base"][name] + rate * silence_hours)
            total_silence = (now - last_user).total_seconds() / 3600
            if total_silence >= 6:
                state["base"]["dejection"] = min(
                    self.spec["dejection"]["neutral"] + caps["dejection"],
                    state["base"]["dejection"] + 0.01 * silence_hours,
                )

    def _save(self, state: dict[str, Any], now: datetime, **fields: Any) -> None:
        assignments = ["state_json=?", "last_updated_at=?", "revision=revision+1"]
        values: list[Any] = [json.dumps(state, separators=(",", ":")), isoformat(now)]
        for name, value in fields.items():
            assignments.append(f"{name}=?")
            values.append(value)
        with self.database.connect() as db:
            db.execute(f"UPDATE affect_state SET {', '.join(assignments)} WHERE id=1", values)

    def classify(self, text: str) -> str:
        folded = text.casefold()
        for label, patterns in self.label_patterns.items():
            if label in LABEL_DELTAS and any(pattern.casefold() in folded for pattern in patterns):
                return label
        return "neutral"

    def apply_label(
        self,
        label: str,
        *,
        now: datetime | None = None,
        source_message_id: int | None = None,
        note: str = "",
        is_user_message: bool = False,
        follow_up_minutes: int | None = None,
        weights: dict[str, float] | None = None,
    ) -> AffectResult:
        if label not in LABEL_DELTAS:
            raise ValueError(f"unsupported affect label: {label}")
        weights = weights if weights is not None else {label: 1.0}
        deltas: dict[str, float] = {}
        for name, weight in weights.items():
            for dimension, delta in LABEL_DELTAS[name].items():
                deltas[dimension] = deltas.get(dimension, 0.0) + weight * delta
        current = now or utc_now()
        row, state = self._load_row(current)
        self._advance(row, state, current)
        window_start = current - timedelta(minutes=self.config.habituation_window_minutes)
        recent = [
            item for item in state.get("recent_labels", []) if parse_time(item.get("at")) >= window_start
        ]
        repeats = sum(item.get("label") == label for item in recent)
        scale = self.config.habituation_factor**repeats
        if is_user_message:
            self._apply_deltas(state, CONTACT_DELTAS)
            soothing = sum(weight for name, weight in weights.items() if name in SOOTHING_LABELS)
            if soothing:
                self._apply_deltas(state, SOOTHING_DELTAS, soothing)
        self._apply_deltas(state, deltas, scale)
        recent.append({"label": label, "at": isoformat(current)})
        state["recent_labels"] = recent[-8:]

        follow_up_at = (
            isoformat(current + timedelta(minutes=follow_up_minutes)) if follow_up_minutes else None
        )
        follow_up_expires = isoformat(current + timedelta(hours=24)) if follow_up_minutes else None
        with self.database.connect() as db:
            cursor = db.execute(
                """INSERT INTO affect_events
                   (label, source_message_id, deltas_json, note, occurred_at,
                    follow_up_at, follow_up_expires_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    label,
                    source_message_id,
                    json.dumps(deltas, separators=(",", ":")),
                    note,
                    isoformat(current),
                    follow_up_at,
                    follow_up_expires,
                ),
            )
            event_id = int(cursor.lastrowid)
        fields: dict[str, Any] = {"last_interaction_at": isoformat(current)}
        if is_user_message:
            fields.update(last_user_message_at=isoformat(current), unanswered_proactive=0)
            with self.database.connect() as db:
                db.execute(
                    "UPDATE proactive_events SET status='cancelled', updated_at=? "
                    "WHERE status IN ('pending','leased')",
                    (isoformat(current),),
                )
        self._save(state, current, **fields)
        with self.database.connect() as db:
            updated = db.execute("SELECT * FROM affect_state WHERE id=1").fetchone()
        return AffectResult(label, self._public_state(state, updated, current), event_id)

    def apply_message(
        self, text: str, message_id: int, now: datetime | None = None
    ) -> AffectResult:
        weights = self.appraisal.weights(text) if self.appraisal else None
        label = max(weights, key=weights.get) if weights else self.classify(text)
        follow_up = 180 if label in NEGATIVE_LABELS else None
        return self.apply_label(
            label,
            now=now,
            source_message_id=message_id,
            is_user_message=True,
            follow_up_minutes=follow_up,
            weights=weights,
            note=json.dumps({"method": "semantic", "weights": weights}, separators=(",", ":"))
            if weights else "",
        )

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or utc_now()
        row, state = self._load_row(current)
        self._advance(row, state, current)
        self._save(state, current)
        with self.database.connect() as db:
            updated = db.execute("SELECT * FROM affect_state WHERE id=1").fetchone()
        return self._public_state(state, updated, current)

    def on_proactive_sent(self, now: datetime | None = None) -> None:
        current = now or utc_now()
        row, state = self._load_row(current)
        self._advance(row, state, current)
        self._apply_deltas(state, {"longing": -0.08, "seeking": -0.05, "anxiety": 0.03})
        self._save(
            state,
            current,
            last_proactive_sent_at=isoformat(current),
            unanswered_proactive=int(row["unanswered_proactive"]) + 1,
        )

    def prompt_context(self, now: datetime | None = None) -> str:
        status = self.status(now)
        values = status["base"]
        deviations = sorted(
            ((abs(values[name] - self.spec[name]["neutral"]), name, values[name]) for name in values),
            reverse=True,
        )
        selected = [(name, value) for deviation, name, value in deviations if deviation >= 0.08][:5]
        if values["fear"] >= 0.05 and not any(name == "fear" for name, _ in selected):
            selected.append(("fear", values["fear"]))
        if not selected:
            return "Affect is near its usual baseline."
        labels = []
        for name, value in selected:
            level = "high" if value >= 0.7 else "elevated" if value >= 0.5 else "noticeable"
            labels.append(f"{name} is {level}")
        return "Affect: " + "; ".join(labels) + "."

    def _public_state(self, state: dict[str, Any], row: Any, now: datetime) -> dict[str, Any]:
        return {
            "base": {key: round(float(value), 4) for key, value in state["base"].items()},
            "mood": {key: round(float(value), 4) for key, value in state["mood"].items()},
            "last_updated_at": isoformat(now),
            "last_user_message_at": row["last_user_message_at"] if row else None,
            "last_proactive_sent_at": row["last_proactive_sent_at"] if row else None,
            "unanswered_proactive": int(row["unanswered_proactive"]) if row else 0,
        }
