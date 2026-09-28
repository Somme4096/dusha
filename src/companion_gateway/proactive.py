from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import emotions as _emotions
from .config import AppConfig
from .serialization import compact_json
from .service import CompanionService
from .timeutil import isoformat, parse_time, utc_now


class ProactiveEngine:
    def __init__(self, service: CompanionService, config: AppConfig):
        self.service = service
        self.database = service.database
        self.config = config
        self.rules = config.proactive
        self.timezone = ZoneInfo(config.timezone)
        effective = _emotions.resolve_emotions(service.affect.config, config.proactive)
        self._emotional = dict(effective["proactive"])
        service.affect.emotions["proactive"] = dict(self._emotional)
        service.affect.emotions_fingerprint = _emotions.fingerprint(effective)

    def _quiet(self, now: datetime) -> bool:
        hour = now.astimezone(self.timezone).hour
        start, end = self.rules.quiet_start_hour, self.rules.quiet_end_hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _sent_today(self, now: datetime) -> int:
        local = now.astimezone(self.timezone)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        with self.database.connect() as db:
            row = db.execute(
                """SELECT COUNT(*) AS count FROM proactive_events
                   WHERE status='sent' AND sent_at>=? AND sent_at<?""",
                (isoformat(day_start), isoformat(day_end)),
            ).fetchone()
        return int(row["count"])

    def evaluate(self, now: datetime | None = None) -> dict[str, Any] | None:
        current = now or utc_now()
        if not self.rules.enabled or self._quiet(current):
            return None
        if not self.config.storage.enabled:
            return None
        state = self.service.affect.status(current)
        if not state["last_user_message_at"]:
            return None
        last_user = parse_time(state["last_user_message_at"])
        silence_minutes = (current - last_user).total_seconds() / 60
        if silence_minutes < self.rules.min_silence_minutes:
            return None
        if state["unanswered_proactive"] >= self.rules.max_unanswered:
            return None
        if self._sent_today(current) >= self.rules.max_per_day:
            return None
        if state["last_proactive_sent_at"]:
            elapsed = (current - parse_time(state["last_proactive_sent_at"])).total_seconds() / 60
            if elapsed < self.rules.cooldown_minutes:
                return None
        route = self.service.latest_route()
        if not route:
            return None
        with self.database.connect() as db:
            active = db.execute(
                """SELECT id FROM proactive_events
                   WHERE status IN ('pending','leased') LIMIT 1""",
            ).fetchone()
            if active:
                return None

        reason = "silence"
        if state["base"]["fear"] >= self._emotional["fear_threshold"]:
            reason = "fear"
        elif state["base"]["longing"] < self._emotional["longing_threshold"]:
            return None

        context = self.service.build_context(
            query="recent conversation unresolved concern fear care",
            harness=route["harness"],
            conversation_id=route["external_id"],
        )
        sent_today = self._sent_today(current)
        local_date = current.astimezone(self.timezone).date()
        dedup_key = ":".join(
            (
                "silence",
                str(state["last_user_message_at"]),
                str(local_date),
                str(sent_today),
            )
        )
        event_id = str(uuid.uuid4())
        payload = {
            "id": event_id,
            "target": {
                "harness": route["harness"],
                "conversation_id": route["external_id"],
                "route": route["route"],
            },
            "reason": reason,
            "generation_instruction": self.service.prompts["proactive_generation_instruction"],
            "context": context,
            "created_at": isoformat(current),
        }
        try:
            with self.database.connect() as db:
                db.execute(
                    """INSERT INTO proactive_events
                       (id, conversation_id, route, reason, dedup_key, payload_json,
                        status, available_at, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,'pending',?,?,?)""",
                    (
                        event_id,
                        route["id"],
                        route["route"],
                        reason,
                        dedup_key,
                        compact_json(payload),
                        isoformat(current),
                        isoformat(current),
                        isoformat(current),
                    ),
                )
        except Exception as error:
            if "UNIQUE constraint failed" in str(error):
                return None
            raise
        return payload

    def poll(
        self,
        consumer: str,
        limit: int = 1,
        now: datetime | None = None,
        harness: str = "",
    ) -> list[dict[str, Any]]:
        if not consumer.strip():
            raise ValueError("consumer is required")
        current = now or utc_now()
        lease_until = current + timedelta(seconds=self.rules.lease_seconds)
        results: list[dict[str, Any]] = []
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE proactive_events SET status='pending', consumer=NULL, lease_until=NULL, updated_at=?
                   WHERE status='leased' AND lease_until<=?""",
                (isoformat(current), isoformat(current)),
            )
            clause = "p.status='pending' AND p.available_at<=?"
            args: list[Any] = [isoformat(current)]
            if harness:
                clause += " AND c.harness=?"
                args.append(harness)
            args.append(max(1, min(limit, 20)))
            rows = db.execute(
                f"""SELECT p.* FROM proactive_events p
                    LEFT JOIN conversations c ON c.id=p.conversation_id
                    WHERE {clause} ORDER BY p.created_at LIMIT ?""",
                args,
            ).fetchall()
            for row in rows:
                updated = db.execute(
                    """UPDATE proactive_events
                       SET status='leased', consumer=?, lease_until=?, attempts=attempts+1, updated_at=?
                       WHERE id=? AND status='pending'""",
                    (consumer, isoformat(lease_until), isoformat(current), row["id"]),
                )
                if updated.rowcount:
                    payload = json.loads(row["payload_json"])
                    payload["lease_until"] = isoformat(lease_until)
                    results.append(payload)
        return results

    def acknowledge(
        self,
        event_id: str,
        consumer: str,
        outcome: str,
        *,
        text: str = "",
        external_id: str = "",
        error: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if outcome not in {"sent", "failed", "release"}:
            raise ValueError("outcome must be sent, failed, or release")
        current = now or utc_now()
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM proactive_events WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise KeyError(event_id)
            if row["status"] != "leased" or row["consumer"] != consumer:
                raise ValueError("event is not leased by this consumer")
            if outcome == "sent":
                db.execute(
                    "UPDATE proactive_events SET status='sent', sent_at=?, updated_at=?, "
                    "lease_until=NULL WHERE id=?",
                    (isoformat(current), isoformat(current), event_id),
                )
            else:
                available = current + timedelta(
                    minutes=self.rules.retry_delay_minutes if outcome == "failed" else 0
                )
                db.execute(
                    """UPDATE proactive_events SET status='pending', consumer=NULL, lease_until=NULL,
                       available_at=?, updated_at=?, last_error=? WHERE id=?""",
                    (isoformat(available), isoformat(current), error[:1000], event_id),
                )
        if outcome == "sent":
            self.service.affect.on_proactive_sent(current)
            if text.strip():
                with self.database.connect() as db:
                    conversation = db.execute(
                        "SELECT * FROM conversations WHERE id=?", (row["conversation_id"],)
                    ).fetchone()
                if conversation:
                    self.service.ingest_message(
                        harness=str(conversation["harness"]),
                        conversation_id=str(conversation["external_id"]),
                        role="assistant",
                        content=text,
                        route=str(conversation["route"]),
                        external_id=external_id or f"proactive:{event_id}",
                        occurred_at=current,
                    )
        return {"id": event_id, "outcome": outcome}
