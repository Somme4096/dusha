from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import emotions as _emotions
from . import prompts as _prompts
from .config import AffectConfig
from .database import Database
from .timeutil import isoformat, parse_time, utc_now

# Backward-compatible view of the packaged default label deltas. The engine
# itself reads the resolved emotions snapshot; this module-level constant keeps
# CLI choices and existing tests working against the default set.
LABEL_DELTAS: dict[str, dict[str, float]] = _emotions.default_emotions()["label_deltas"]


@dataclass(slots=True)
class AffectResult:
    label: str
    state: dict[str, Any]
    event_id: int | None


class AffectClassificationConflict(RuntimeError):
    pass


class AffectEngine:
    def __init__(
        self, database: Database, config: AffectConfig, prompts: dict[str, Any] | None = None
    ):
        self.database = database
        self.config = config
        self._lock = threading.RLock()
        self.emotions = _emotions.resolve_emotions(config)
        self.emotions_version = str(self.emotions["emotion_version"])
        self.emotions_fingerprint = _emotions.fingerprint(self.emotions)
        self.label_patterns = dict(self.emotions["label_patterns"])
        self.spec = {name: values.copy() for name, values in self.emotions["dimensions"].items()}
        value_range = self.emotions["value_range"]
        self.value_min = float(value_range["min"])
        self.value_max = float(value_range["max"])
        self.affect_presentation = dict(
            (prompts or _prompts.default_prompts())["affect_presentation"]
        )

    def initial_state(self) -> dict[str, Any]:
        base = {name: values["neutral"] for name, values in self.spec.items()}
        return {"base": base.copy(), "mood": base.copy(), "recent_labels": []}

    def _ensure(self, db: Any, now: datetime) -> None:
        state = self.initial_state()
        db.execute(
            """INSERT OR IGNORE INTO affect_state
               (id, state_json, last_updated_at, last_interaction_at)
               VALUES(1,?,?,?)""",
            (json.dumps(state, separators=(",", ":")), isoformat(now), isoformat(now)),
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
            if name not in base:
                continue
            delta = nominal * scale
            current = float(base[name])
            effective = (
                delta * impact * (1 - current) if delta > 0 else delta * impact * current
            )
            next_value = max(current + effective, self.spec[name]["floor"])
            if delta < 0 and name in negative:
                next_value = max(next_value, min(current, float(mood[name])))
            base[name] = self._clamp(next_value, self.spec[name]["floor"])

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
        values: list[Any] = [json.dumps(state, separators=(",", ":")), isoformat(now)]
        for name, value in fields.items():
            assignments.append(f"{name}=?")
            values.append(value)
        db.execute(f"UPDATE affect_state SET {', '.join(assignments)} WHERE id=1", values)

    def classify(self, text: str) -> str:
        folded = text.casefold()
        for label, patterns in self.label_patterns.items():
            if label in self.emotions["label_deltas"] and any(
                pattern.casefold() in folded for pattern in patterns
            ):
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
    ) -> AffectResult:
        if label not in self.emotions["label_deltas"]:
            raise ValueError(f"unsupported affect label: {label}")
        current = now or utc_now()
        with self._lock, self.database.connect() as db:
            return self._apply_label_in_db(
                db,
                label,
                current=current,
                event_time=current,
                source_message_id=source_message_id,
                note=note,
                is_user_message=is_user_message,
                follow_up_minutes=follow_up_minutes,
            )

    def _apply_label_in_db(
        self,
        db: Any,
        label: str,
        *,
        current: datetime,
        event_time: datetime,
        source_message_id: int | None,
        note: str,
        is_user_message: bool,
        follow_up_minutes: int | None,
        user_contact_recorded: bool = False,
    ) -> AffectResult:
        row, state = self._load_row(db, current)
        self._advance(row, state, current)
        affect = self.emotions["affect"]
        window_start = current - timedelta(minutes=affect["habituation_window_minutes"])
        recent = [
            item for item in state.get("recent_labels", []) if parse_time(item.get("at")) >= window_start
        ]
        repeats = sum(item.get("label") == label for item in recent)
        scale = affect["habituation_factor"] ** repeats
        if is_user_message:
            self._apply_deltas(state, self.emotions["contact_deltas"])
            if label in set(self.emotions["soothing_labels"]):
                self._apply_deltas(state, self.emotions["soothing_deltas"])
        self._apply_deltas(state, self.emotions["label_deltas"][label], scale)
        recent.append({"label": label, "at": isoformat(current)})
        state["recent_labels"] = recent[-self.emotions["recent_labels_limit"] :]

        follow_up_at = (
            isoformat(current + timedelta(minutes=follow_up_minutes)) if follow_up_minutes else None
        )
        follow_up_expires = (
            isoformat(
                current + timedelta(hours=self.emotions["follow_up_expiration_hours"])
            )
            if follow_up_minutes
            else None
        )
        cursor = db.execute(
            """INSERT INTO affect_events
               (label, source_message_id, deltas_json, note, occurred_at,
                follow_up_at, follow_up_expires_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                label,
                source_message_id,
                json.dumps(self.emotions["label_deltas"][label], separators=(",", ":")),
                note,
                isoformat(event_time),
                follow_up_at,
                follow_up_expires,
            ),
        )
        event_id = int(cursor.lastrowid)
        fields: dict[str, Any] = {}
        if is_user_message and not user_contact_recorded:
            fields.update(last_user_message_at=isoformat(current), unanswered_proactive=0)
            fields["last_interaction_at"] = isoformat(current)
            db.execute(
                "UPDATE proactive_events SET status='cancelled', updated_at=? "
                "WHERE status IN ('pending','leased')",
                (isoformat(current),),
            )
        self._save(db, state, current, **fields)
        updated = db.execute("SELECT * FROM affect_state WHERE id=1").fetchone()
        return AffectResult(label, self._public_state(state, updated, current), event_id)

    def stage_message(self, message_id: int) -> dict[str, Any]:
        with self._lock, self.database.connect() as db:
            message = db.execute(
                "SELECT id, role, text, occurred_at FROM messages WHERE id=?",
                (message_id,),
            ).fetchone()
            if not message:
                raise KeyError("source message not found")
            if message["role"] != "user":
                raise ValueError("affect classification requires a user message")
            occurred = parse_time(message["occurred_at"])
            automatic_label = self.classify(str(message["text"]))
            inserted = db.execute(
                """INSERT OR IGNORE INTO affect_classifications
                   (source_message_id, automatic_label, status, occurred_at, finalize_after)
                   VALUES(?,?, 'pending', ?,?)""",
                (
                    message_id,
                    automatic_label,
                    isoformat(occurred),
                    isoformat(
                        occurred
                        + timedelta(
                            seconds=self.emotions["affect"]["classification_fallback_seconds"]
                        )
                    ),
                ),
            )
            if inserted.rowcount:
                self._ensure(db, occurred)
                occurred_text = isoformat(occurred)
                db.execute(
                    """UPDATE affect_state SET
                       last_user_message_at=CASE
                         WHEN last_user_message_at IS NULL OR last_user_message_at < ? THEN ?
                         ELSE last_user_message_at END,
                       last_interaction_at=CASE
                         WHEN last_interaction_at IS NULL OR last_interaction_at < ? THEN ?
                         ELSE last_interaction_at END,
                       unanswered_proactive=0
                       WHERE id=1""",
                    (occurred_text, occurred_text, occurred_text, occurred_text),
                )
                db.execute(
                    "UPDATE proactive_events SET status='cancelled', updated_at=? "
                    "WHERE status IN ('pending','leased')",
                    (occurred_text,),
                )
            row = db.execute(
                "SELECT * FROM affect_classifications WHERE source_message_id=?",
                (message_id,),
            ).fetchone()
        return self._public_classification(row)

    def record_agent_label(
        self, message_id: int, label: str, now: datetime | None = None
    ) -> dict[str, Any]:
        return self._resolve_classification(message_id, "agent", label, now or utc_now())

    def record_provided_label(
        self, message_id: int, label: str, now: datetime | None = None
    ) -> dict[str, Any]:
        return self._resolve_classification(message_id, "provided", label, now or utc_now())

    def finalize_automatic(
        self, message_id: int, now: datetime | None = None
    ) -> dict[str, Any]:
        return self._resolve_classification(message_id, "automatic", None, now or utc_now())

    def finalize_conversation(
        self, conversation_id: int, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            message_ids = [
                int(row["source_message_id"])
                for row in db.execute(
                    """SELECT c.source_message_id FROM affect_classifications c
                       JOIN messages m ON m.id=c.source_message_id
                       WHERE c.status='pending' AND m.conversation_id=?
                       ORDER BY c.occurred_at, c.source_message_id""",
                    (conversation_id,),
                ).fetchall()
            ]
        current = now or utc_now()
        return [self.finalize_automatic(message_id, current) for message_id in message_ids]

    def finalize_due(
        self, now: datetime | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        current = now or utc_now()
        with self.database.connect() as db:
            message_ids = [
                int(row["source_message_id"])
                for row in db.execute(
                    """SELECT source_message_id FROM affect_classifications
                       WHERE status='pending' AND finalize_after <= ?
                       ORDER BY finalize_after, source_message_id LIMIT ?""",
                    (isoformat(current), max(1, limit)),
                ).fetchall()
            ]
        return [self.finalize_automatic(message_id, current) for message_id in message_ids]

    def classification(self, message_id: int) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM affect_classifications WHERE source_message_id=?",
                (message_id,),
            ).fetchone()
        return self._public_classification(row) if row else None

    def _resolve_classification(
        self,
        message_id: int,
        decision_source: str,
        label: str | None,
        current: datetime,
    ) -> dict[str, Any]:
        if label is not None and label not in self.emotions["label_deltas"]:
            raise ValueError(f"unsupported affect label: {label}")
        with self._lock, self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM affect_classifications WHERE source_message_id=?",
                (message_id,),
            ).fetchone()
            if not row:
                raise KeyError("affect classification not found")
            if row["status"] == "applied":
                if decision_source == "agent" and (
                    row["decision_source"] != "agent" or row["agent_label"] != label
                ):
                    raise AffectClassificationConflict("affect classification is already finalized")
                return self._public_classification(row)

            chosen_label = label if label is not None else str(row["automatic_label"])
            negative_labels = set(self.emotions["negative_labels"])
            follow_up = (
                self.emotions["negative_follow_up_minutes"] if chosen_label in negative_labels else None
            )
            result = self._apply_label_in_db(
                db,
                chosen_label,
                current=current,
                event_time=parse_time(row["occurred_at"]),
                source_message_id=message_id,
                note="",
                is_user_message=True,
                follow_up_minutes=follow_up,
                user_contact_recorded=True,
            )
            db.execute(
                """UPDATE affect_classifications SET
                   agent_label=?, chosen_label=?, decision_source=?, status='applied',
                   resolved_at=?, affect_event_id=?
                   WHERE source_message_id=? AND status='pending'""",
                (
                    label if decision_source == "agent" else None,
                    chosen_label,
                    decision_source,
                    isoformat(current),
                    result.event_id,
                    message_id,
                ),
            )
            updated = db.execute(
                "SELECT * FROM affect_classifications WHERE source_message_id=?",
                (message_id,),
            ).fetchone()
        return self._public_classification(updated)

    @staticmethod
    def _public_classification(row: Any) -> dict[str, Any]:
        return {
            "source_message_id": int(row["source_message_id"]),
            "automatic_label": str(row["automatic_label"]),
            "agent_label": str(row["agent_label"]) if row["agent_label"] is not None else None,
            "label": str(row["chosen_label"]) if row["chosen_label"] is not None else None,
            "decision_source": (
                str(row["decision_source"]) if row["decision_source"] is not None else None
            ),
            "status": str(row["status"]),
            "occurred_at": str(row["occurred_at"]),
            "finalize_after": str(row["finalize_after"]),
            "resolved_at": str(row["resolved_at"]) if row["resolved_at"] is not None else None,
            "event_id": int(row["affect_event_id"]) if row["affect_event_id"] is not None else None,
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
        """Render the affect presentation text for a status snapshot."""
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