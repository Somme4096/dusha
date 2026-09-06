from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import uvicorn

from .affect import LABEL_DELTAS
from .api import create_app
from .config import load_config
from .proactive import ProactiveEngine
from .service import CompanionService


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="companion-gateway")
    root.add_argument("--config", default=None, help="Configuration file path")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("serve", help="Run the HTTP service")
    commands.add_parser("health", help="Check the database")

    backup = commands.add_parser("backup", help="Create a consistent SQLite backup")
    backup.add_argument("destination")

    memory = commands.add_parser("memory", help="Inspect canonical memory")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    search = memory_commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--companion", default="")
    search.add_argument("--limit", type=int, default=8)
    show = memory_commands.add_parser("show")
    show.add_argument("message_id", type=int)
    recent = memory_commands.add_parser("recent")
    recent.add_argument("--companion", default="")
    recent.add_argument("--limit", type=int, default=20)
    memory_commands.add_parser("reindex")

    affect = commands.add_parser("affect", help="Inspect or update affect")
    affect_commands = affect.add_subparsers(dest="affect_command", required=True)
    affect_show = affect_commands.add_parser("show")
    affect_show.add_argument("--companion", default="")
    affect_event = affect_commands.add_parser("event")
    affect_event.add_argument("label", choices=sorted(LABEL_DELTAS))
    affect_event.add_argument("--companion", default="")
    affect_event.add_argument("--note", default="")
    affect_event.add_argument("--follow-up-minutes", type=int, default=None)

    proactive = commands.add_parser("proactive", help="Manage proactive events")
    proactive_commands = proactive.add_subparsers(dest="proactive_command", required=True)
    evaluate = proactive_commands.add_parser("evaluate")
    evaluate.add_argument("--companion", default="")
    poll = proactive_commands.add_parser("poll")
    poll.add_argument("--consumer", default="cli")
    poll.add_argument("--companion", default="")
    poll.add_argument("--limit", type=int, default=1)
    ack = proactive_commands.add_parser("ack")
    ack.add_argument("event_id")
    ack.add_argument("outcome", choices=["sent", "failed", "release"])
    ack.add_argument("--consumer", default="cli")
    ack.add_argument("--text", default="")
    ack.add_argument("--error", default="")
    return root


def main() -> None:
    args = parser().parse_args()
    cfg = load_config(args.config)
    if args.command == "serve":
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")
        return

    service = CompanionService(cfg)
    proactive = ProactiveEngine(service, cfg)
    companion = getattr(args, "companion", "") or cfg.default_companion_id

    if args.command == "health":
        _print({"database": service.database.integrity_check(), "companions": service.companion_ids()})
    elif args.command == "backup":
        _print({"backup": service.backup(args.destination)})
    elif args.command == "memory":
        if args.memory_command == "search":
            _print({"results": service.memory.search(companion, args.query, args.limit)})
        elif args.memory_command == "show":
            result = service.memory.get(args.message_id)
            if result is None:
                print("message not found", file=sys.stderr)
                raise SystemExit(1)
            _print(result)
        elif args.memory_command == "recent":
            _print({"messages": service.memory.recent(companion, limit=args.limit)})
        else:
            service.memory.rebuild_index()
            _print({"status": "rebuilt"})
    elif args.command == "affect":
        if args.affect_command == "show":
            _print(service.affect.status(companion))
        else:
            result = service.affect.apply_label(
                companion,
                args.label,
                note=args.note,
                follow_up_minutes=args.follow_up_minutes,
            )
            _print({"label": result.label, "event_id": result.event_id, "state": result.state})
    elif args.command == "proactive":
        if args.proactive_command == "evaluate":
            _print({"event": proactive.evaluate(companion)})
        elif args.proactive_command == "poll":
            _print({"events": proactive.poll(args.consumer, companion, args.limit)})
        else:
            _print(
                proactive.acknowledge(
                    args.event_id,
                    args.consumer,
                    args.outcome,
                    text=args.text,
                    error=args.error,
                )
            )


if __name__ == "__main__":
    main()
