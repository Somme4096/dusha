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
    search.add_argument("--limit", type=int, default=8)
    show = memory_commands.add_parser("show")
    show.add_argument("message_id", type=int)
    recent = memory_commands.add_parser("recent")
    recent.add_argument("--limit", type=int, default=20)
    memory_commands.add_parser("reindex")
    index = memory_commands.add_parser("index", help="Manage the disposable semantic index")
    index_commands = index.add_subparsers(dest="index_command", required=True)
    index_commands.add_parser("status")
    backfill = index_commands.add_parser("backfill")
    backfill.add_argument("--limit", type=int, default=None)
    backfill.add_argument("--force", action="store_true")
    index_commands.add_parser("rebuild")

    evergreen = commands.add_parser("evergreen", help="Inspect or update evergreen facts")
    evergreen_commands = evergreen.add_subparsers(dest="evergreen_command", required=True)
    evergreen_list = evergreen_commands.add_parser("list")
    evergreen_list.add_argument("--include-inactive", action="store_true")
    evergreen_list.add_argument("--due-only", action="store_true")
    evergreen_list.add_argument("--limit", type=int, default=100)
    evergreen_history = evergreen_commands.add_parser("history")
    evergreen_history.add_argument("fact_id")
    evergreen_remember = evergreen_commands.add_parser("remember")
    evergreen_remember.add_argument("key")
    evergreen_remember.add_argument("text")
    evergreen_remember.add_argument("--priority", type=int, default=50)
    evergreen_remember.add_argument("--source-message-id", type=int)
    evergreen_remember.add_argument("--reason", default="")
    evergreen_remember.add_argument("--review-after")
    evergreen_remember.add_argument("--expires-at")
    evergreen_revise = evergreen_commands.add_parser("revise")
    evergreen_revise.add_argument("fact_id")
    evergreen_revise.add_argument("expected_revision", type=int)
    evergreen_revise.add_argument("text")
    evergreen_revise.add_argument("--priority", type=int)
    evergreen_revise.add_argument("--source-message-id", type=int)
    evergreen_revise.add_argument("--reason", default="")
    review_group = evergreen_revise.add_mutually_exclusive_group()
    review_group.add_argument("--review-after")
    review_group.add_argument("--clear-review-after", action="store_true")
    expiry_group = evergreen_revise.add_mutually_exclusive_group()
    expiry_group.add_argument("--expires-at")
    expiry_group.add_argument("--clear-expires-at", action="store_true")
    evergreen_forget = evergreen_commands.add_parser("forget")
    evergreen_forget.add_argument("fact_id")
    evergreen_forget.add_argument("expected_revision", type=int)
    evergreen_forget.add_argument("--source-message-id", type=int)
    evergreen_forget.add_argument("--reason", required=True)

    affect = commands.add_parser("affect", help="Inspect or update affect")
    affect_commands = affect.add_subparsers(dest="affect_command", required=True)
    affect_commands.add_parser("show")
    affect_event = affect_commands.add_parser("event")
    affect_event.add_argument("label", choices=sorted(LABEL_DELTAS))
    affect_event.add_argument("--note", default="")
    affect_event.add_argument("--follow-up-minutes", type=int, default=None)

    proactive = commands.add_parser("proactive", help="Manage proactive events")
    proactive_commands = proactive.add_subparsers(dest="proactive_command", required=True)
    proactive_commands.add_parser("evaluate")
    poll = proactive_commands.add_parser("poll")
    poll.add_argument("--consumer", default="cli")
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

    if args.command == "health":
        _print({"database": service.database.integrity_check()})
    elif args.command == "backup":
        _print({"backup": service.backup(args.destination)})
    elif args.command == "memory":
        if args.memory_command == "search":
            _print({"results": service.memory.search(args.query, args.limit)})
        elif args.memory_command == "show":
            result = service.memory.get(args.message_id)
            if result is None:
                print("message not found", file=sys.stderr)
                raise SystemExit(1)
            _print(result)
        elif args.memory_command == "recent":
            _print({"messages": service.memory.recent(limit=args.limit)})
        elif args.memory_command == "index":
            if args.index_command == "status":
                _print(service.semantic.status())
            elif args.index_command == "backfill":
                _print(service.semantic.backfill_once(args.limit, force=args.force))
            else:
                _print(service.semantic.rebuild_chunks())
        else:
            service.memory.rebuild_index()
            _print({"status": "rebuilt"})
    elif args.command == "evergreen":
        if args.evergreen_command == "list":
            _print(
                {
                    "facts": service.evergreen.list_current(
                        include_inactive=args.include_inactive,
                        due_only=args.due_only,
                        limit=args.limit,
                    )
                }
            )
        elif args.evergreen_command == "history":
            _print({"revisions": service.evergreen.history(args.fact_id)})
        elif args.evergreen_command == "remember":
            _print(
                {
                    "fact": service.evergreen.remember(
                        key=args.key,
                        text=args.text,
                        priority=args.priority,
                        source_message_id=args.source_message_id,
                        reason=args.reason,
                        review_after=args.review_after,
                        expires_at=args.expires_at,
                        created_by="operator",
                    )
                }
            )
        elif args.evergreen_command == "revise":
            revise_kwargs = {
                "fact_id": args.fact_id,
                "expected_revision": args.expected_revision,
                "text": args.text,
                "priority": args.priority,
                "source_message_id": args.source_message_id,
                "reason": args.reason,
                "created_by": "operator",
            }
            if args.clear_review_after:
                revise_kwargs["review_after"] = None
            elif args.review_after is not None:
                revise_kwargs["review_after"] = args.review_after
            if args.clear_expires_at:
                revise_kwargs["expires_at"] = None
            elif args.expires_at is not None:
                revise_kwargs["expires_at"] = args.expires_at
            _print({"fact": service.evergreen.revise(**revise_kwargs)})
        else:
            _print(
                {
                    "fact": service.evergreen.forget(
                        fact_id=args.fact_id,
                        expected_revision=args.expected_revision,
                        reason=args.reason,
                        source_message_id=args.source_message_id,
                        created_by="operator",
                    )
                }
            )
    elif args.command == "affect":
        if args.affect_command == "show":
            _print(service.affect.status())
        else:
            result = service.affect.apply_label(
                args.label,
                note=args.note,
                follow_up_minutes=args.follow_up_minutes,
            )
            _print({"label": result.label, "event_id": result.event_id, "state": result.state})
    elif args.command == "proactive":
        if args.proactive_command == "evaluate":
            _print({"event": proactive.evaluate()})
        elif args.proactive_command == "poll":
            _print({"events": proactive.poll(args.consumer, args.limit)})
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
    service.close()


if __name__ == "__main__":
    main()
