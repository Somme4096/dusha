from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from . import prompts as _prompts
from . import schema as _schema
from .commands import arg, option
from .config import ProactiveConfig
from .database import Database
from .emotions import with_thresholds
from .interfaces import GatewayInput, call, errors
from .serialization import compact_json
from .timeutil import isoformat, parse_time, utc_now

name = "proactive"
_PROMPTS = {
    "framing": {"ruling": str, "ladder": list, "approach": str, "context_query": str},
    "generation_instruction": {"base": str, "variants": list},
}
_TEMPLATES = {
    "framing.ruling": {"dimension": "", "value": 0.0, "neutral": 0.0, "deviation": 0.0},
    "framing.approach": {"variant": ""},
}
inject = ("affect", "companion", "database", "settings", "prompts", "api", "cli")

SCHEMA = """
CREATE TABLE IF NOT EXISTS proactive_events (
    id TEXT PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    route TEXT NOT NULL,
    reason TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    consumer TEXT,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS proactive_events_poll
ON proactive_events(status, available_at, created_at);
"""


class AckInput(GatewayInput):
    consumer: str
    outcome: str
    text: str = ""
    external_id: str = ""
    error: str = ""


def format_silence(minutes: float) -> str:
    total = max(0, int(round(minutes)))
    if total < 1:
        return "less than a minute"
    if total < 60:
        unit = "minute" if total == 1 else "minutes"
        return f"{total} {unit}"
    if total < 24 * 60:
        hours, remainder = divmod(total, 60)
        hour_unit = "hour" if hours == 1 else "hours"
        if remainder == 0:
            return f"{hours} {hour_unit}"
        minute_unit = "minute" if remainder == 1 else "minutes"
        return f"{hours} {hour_unit} {remainder} {minute_unit}"
    days, remainder = divmod(total, 24 * 60)
    day_unit = "day" if days == 1 else "days"
    if remainder == 0:
        return f"{days} {day_unit}"
    hours = remainder // 60
    if hours == 0:
        minute_unit = "minute" if remainder == 1 else "minutes"
        return f"{days} {day_unit} {remainder} {minute_unit}"
    hour_unit = "hour" if hours == 1 else "hours"
    return f"{days} {day_unit} {hours} {hour_unit}"


def select_proactive_variant(spec: dict[str, Any], sent_today: int) -> tuple[int, str]:
    variants = list(spec["variants"])
    index = int(sent_today) % len(variants)
    return index, str(variants[index])


def ruling_feeling(base: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    top = {"dimension": "", "value": 0.0, "neutral": 0.0, "deviation": 0.0}
    for name, value in base.items():
        neutral = float(spec.get(name, {}).get("neutral", value))
        current = float(value)
        deviation = abs(current - neutral)
        if deviation > float(top["deviation"]):
            top = {
                "dimension": name,
                "value": round(current, 4),
                "neutral": neutral,
                "deviation": round(deviation, 4),
            }
    return top


def ladder_line(ladder: list[str], unanswered: int) -> tuple[int, str]:
    stage = min(max(0, int(unanswered)), len(ladder) - 1)
    return stage, ladder[stage]


def build_proactive_instruction(
    base: str, variant: str, framing: dict[str, Any], feeling: dict[str, Any], ladder: str
) -> str:
    ruling = framing["ruling"].format(**feeling)
    approach = framing["approach"].format(variant=variant.strip())
    return f"{base.rstrip()}\n{ruling}\n{ladder}\n{approach}"


class ProactiveEngine:
    name = name

    def __init__(
        self,
        database: Database,
        config: ProactiveConfig,
        timezone: str,
        prompts: dict[str, Any],
        affect: Any,
        companion: Any,
    ):
        self.database = database
        database.migrate(SCHEMA)
        self.rules = config
        self.timezone = ZoneInfo(timezone)
        self.prompts = prompts
        self.text = _prompts.section(prompts, name, _PROMPTS, _TEMPLATES)
        self.affect = affect
        self.companion = companion
        self.triggers = with_thresholds(affect.emotions, config.thresholds)["proactive"]["triggers"]

    def on_message(self, message: dict[str, Any]) -> None:
        if message.get("role") != "user":
            return None
        with self.database.connect() as db:
            db.execute(
                "UPDATE proactive_events SET status='cancelled', updated_at=? "
                "WHERE status IN ('pending','leased')",
                (message["occurred_at"],),
            )
        return None

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
        if self._quiet(current):
            return None
        state = self.affect.status(current)
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
        route = self.companion.latest_route()
        if not route:
            return None
        with self.database.connect() as db:
            active = db.execute(
                """SELECT id FROM proactive_events
                   WHERE status IN ('pending','leased') LIMIT 1""",
            ).fetchone()
            if active:
                return None

        reason = next(
            (
                trigger["reason"]
                for trigger in self.triggers
                if state["base"][trigger["dimension"]] >= trigger["threshold"]
            ),
            None,
        )
        if reason is None:
            return None

        framing = self.text["framing"]
        context = self.companion.build_context(
            query=str(framing["context_query"]),
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
        silence_text = format_silence(silence_minutes)
        spec = self.text["generation_instruction"]
        base = str(spec["base"])
        variant_index, variant = select_proactive_variant(spec, sent_today)
        feeling = ruling_feeling(state["base"], self.affect.spec)
        ladder_stage, ladder = ladder_line(framing["ladder"], int(state["unanswered_proactive"]))
        generation_instruction = build_proactive_instruction(base, variant, framing, feeling, ladder)
        payload = {
            "id": event_id,
            "target": {
                "harness": route["harness"],
                "conversation_id": route["external_id"],
                "route": route["route"],
            },
            "reason": reason,
            "generation_instruction": generation_instruction,
            "generation_base": base,
            "generation_variant": variant,
            "generation_variant_index": variant_index,
            "ruling_feeling": feeling,
            "ladder_stage": ladder_stage,
            "unanswered_proactive": int(state["unanswered_proactive"]),
            "silence_minutes": round(silence_minutes, 2),
            "silence_text": silence_text,
            "last_user_message_at": state["last_user_message_at"],
            "evaluated_at": isoformat(current),
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
            self.affect.on_proactive_sent(current)
            if text.strip():
                with self.database.connect() as db:
                    conversation = db.execute(
                        "SELECT * FROM conversations WHERE id=?", (row["conversation_id"],)
                    ).fetchone()
                if conversation:
                    self.companion.ingest_message(
                        harness=str(conversation["harness"]),
                        conversation_id=str(conversation["external_id"]),
                        role="assistant",
                        content=text,
                        route=str(conversation["route"]),
                        external_id=external_id or f"proactive:{event_id}",
                        occurred_at=current,
                    )
        return {"id": event_id, "outcome": outcome}


def apply(ctx: Any, config: ProactiveConfig) -> None:
    engine = ProactiveEngine(
        ctx.database, config, ctx.settings.timezone, ctx.prompts, ctx.affect, ctx.companion
    )
    ctx.provide("proactive", engine)
    ctx.on("message", engine.on_message)
    ctx.interval(max(5, config.poll_interval_seconds), engine.evaluate)
    _routes(ctx.api, engine)
    _commands(ctx.cli, engine)


def _routes(router: APIRouter, engine: ProactiveEngine) -> None:
    @router.post(
        "/state/v1/proactive/evaluate",
        tags=["state"],
        response_model=_schema.ProactiveEvaluateResponse,
        responses=errors(503),
    )
    async def evaluate_proactive() -> dict[str, Any]:
        return {"event": await call(engine.evaluate)}

    @router.get(
        "/state/v1/proactive/events",
        tags=["state"],
        response_model=_schema.ProactivePollResponse,
        summary="Poll and lease proactive events",
        responses=errors(503),
    )
    async def poll_proactive(
        consumer: str = Query(..., min_length=1),
        harness: str = "",
        limit: int = Query(1, ge=1, le=20),
    ) -> dict[str, Any]:
        return {"events": await call(engine.poll, consumer, limit, None, harness)}

    @router.post(
        "/state/v1/proactive/events/{event_id}/ack",
        tags=["state"],
        response_model=_schema.AckResponse,
        responses=errors(404, 409, 503),
    )
    async def acknowledge_proactive(event_id: str, body: AckInput) -> dict[str, Any]:
        return await call(
            engine.acknowledge,
            event_id,
            body.consumer,
            body.outcome,
            text=body.text,
            external_id=body.external_id,
            error=body.error,
            errors=(KeyError, ValueError),
            not_found="event not found",
            invalid=409,
        )


def _commands(cli: Any, engine: ProactiveEngine) -> None:
    proactive = cli.group("proactive", "Manage proactive events")
    proactive.command("evaluate", lambda: {"event": engine.evaluate()}, help="Run one evaluation pass")
    proactive.command(
        "poll",
        lambda consumer, limit: {"events": engine.poll(consumer, limit)},
        help="Lease pending events",
        args=[option("consumer", default="cli"), option("limit", int, default=1)],
    )
    proactive.command(
        "ack",
        lambda event_id, outcome, consumer, text, error: engine.acknowledge(
            event_id, consumer, outcome, text=text, error=error
        ),
        help="Acknowledge a leased event",
        args=[
            arg("event_id"),
            arg("outcome", choices=("sent", "failed", "release")),
            option("consumer", default="cli"),
            option("text", default=""),
            option("error", default=""),
        ],
    )
