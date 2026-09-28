from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

import uvicorn

from .api import create_app
from .config import load_config
from .memory_plugin import (
    FactHistoryRequest,
    ForgetFactRequest,
    GetMessageRequest,
    ListFactsRequest,
    RememberFactRequest,
    ReviseFactRequest,
    RecentMessagesRequest,
    SearchRequest,
)
from .proactive import ProactiveEngine
from .service import CompanionService

Handler = Callable[[CompanionService, ProactiveEngine, argparse.Namespace], None]


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

    affect = commands.add_parser("affect", help="Inspect affect")
    affect_commands = affect.add_subparsers(dest="affect_command", required=True)
    affect_commands.add_parser("show")

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




def _cmd_health(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _print({"database": service.database.integrity_check()})


def _cmd_backup(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _print({"backup": service.backup(args.destination)})


def _cmd_memory(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _MEMORY_HANDLERS[args.memory_command](service, proactive, args)


def _memory_search(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    if service._memory_fallback is not None:
        _print({"results": service._memory_fallback.search(args.query, args.limit)})
    else:
        _print({"results": service.memory.search(SearchRequest(args.query, args.limit)).results})


def _memory_show(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    if service._memory_fallback is not None:
        result = service._memory_fallback.get(args.message_id)
    else:
        result = service.memory.get(GetMessageRequest(args.message_id)).message
    if result is None:
        print("message not found", file=sys.stderr)
        raise SystemExit(1)
    _print(result)


def _memory_recent(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    if service._memory_fallback is not None:
        _print({"messages": service._memory_fallback.recent(limit=args.limit)})
    else:
        _print({"messages": service.memory.recent(RecentMessagesRequest("", "", args.limit)).messages})


def _memory_reindex(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _print(service.memory_reindex())


def _memory_index(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _MEMORY_INDEX_HANDLERS[args.index_command](service, proactive, args)


def _index_status(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _print(service.memory_index_status())


def _index_backfill(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _print(service.memory_index_backfill(args.limit or 100, args.force))


def _index_rebuild(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _print(service.memory_index_rebuild())


_MEMORY_INDEX_HANDLERS: dict[str, Handler] = {
    "status": _index_status,
    "backfill": _index_backfill,
    "rebuild": _index_rebuild,
}

_MEMORY_HANDLERS: dict[str, Handler] = {
    "search": _memory_search,
    "show": _memory_show,
    "recent": _memory_recent,
    "index": _memory_index,
    "reindex": _memory_reindex,
}


def _cmd_evergreen(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _EVERGREEN_HANDLERS[args.evergreen_command](service, proactive, args)


def _evergreen_list(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    if service._evergreen_fallback is not None:
        _print({"facts": service._evergreen_fallback.list_current(include_inactive=args.include_inactive, due_only=args.due_only, limit=args.limit)})
        return
    memory_provider = service.memory
    _print(
        {
            "facts": memory_provider.list_current(
                ListFactsRequest(args.include_inactive, args.due_only, args.limit)
            ).facts
        }
    )


def _evergreen_history(
    service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace
) -> None:
    if service._evergreen_fallback is not None:
        _print({"revisions": service._evergreen_fallback.history(args.fact_id)})
        return
    _print({"revisions": service.memory.history(FactHistoryRequest(args.fact_id)).revisions})


def _evergreen_remember(
    service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace
) -> None:
    if service._evergreen_fallback is not None:
        _print({"fact": service._evergreen_fallback.remember(key=args.key, text=args.text, priority=args.priority, source_message_id=args.source_message_id, reason=args.reason, review_after=args.review_after, expires_at=args.expires_at, created_by="operator")})
        return
    memory_provider = service.memory
    _print(
        {
            "fact": memory_provider.remember(RememberFactRequest(
                key=args.key,
                text=args.text,
                priority=args.priority,
                source_message_id=args.source_message_id,
                reason=args.reason,
                review_after=args.review_after,
                expires_at=args.expires_at,
                created_by="operator",
            )).fact
        }
    )


def _evergreen_revise(
    service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace
) -> None:
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
    if service._evergreen_fallback is not None:
        _print({"fact": service._evergreen_fallback.revise(**revise_kwargs)})
        return
    _print({"fact": service.memory.revise(ReviseFactRequest(**revise_kwargs)).fact})


def _evergreen_forget(
    service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace
) -> None:
    if service._evergreen_fallback is not None:
        _print({"fact": service._evergreen_fallback.forget(fact_id=args.fact_id, expected_revision=args.expected_revision, reason=args.reason, source_message_id=args.source_message_id, created_by="operator")})
        return
    memory_provider = service.memory
    _print(
        {
            "fact": memory_provider.forget(ForgetFactRequest(
                fact_id=args.fact_id,
                expected_revision=args.expected_revision,
                reason=args.reason,
                source_message_id=args.source_message_id,
                created_by="operator",
            )).fact
        }
    )


_EVERGREEN_HANDLERS: dict[str, Handler] = {
    "list": _evergreen_list,
    "history": _evergreen_history,
    "remember": _evergreen_remember,
    "revise": _evergreen_revise,
    "forget": _evergreen_forget,
}


def _cmd_affect(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _AFFECT_HANDLERS[args.affect_command](service, proactive, args)


def _affect_show(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _print(service.affect.status())


_AFFECT_HANDLERS: dict[str, Handler] = {
    "show": _affect_show,
}


def _cmd_proactive(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _PROACTIVE_HANDLERS[args.proactive_command](service, proactive, args)


def _proactive_evaluate(
    service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace
) -> None:
    _print({"event": proactive.evaluate()})


def _proactive_poll(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _print({"events": proactive.poll(args.consumer, args.limit)})


def _proactive_ack(service: CompanionService, proactive: ProactiveEngine, args: argparse.Namespace) -> None:
    _print(
        proactive.acknowledge(
            args.event_id,
            args.consumer,
            args.outcome,
            text=args.text,
            error=args.error,
        )
    )


_PROACTIVE_HANDLERS: dict[str, Handler] = {
    "evaluate": _proactive_evaluate,
    "poll": _proactive_poll,
    "ack": _proactive_ack,
}

_COMMANDS: dict[str, Handler] = {
    "health": _cmd_health,
    "backup": _cmd_backup,
    "memory": _cmd_memory,
    "evergreen": _cmd_evergreen,
    "affect": _cmd_affect,
    "proactive": _cmd_proactive,
}


def main() -> None:
    args = parser().parse_args()
    cfg = load_config(args.config)
    if args.command == "serve":
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")
        return

    service = CompanionService(cfg)
    if args.command in {"memory", "evergreen"} and not service.storage_enabled:
        print("built-in message storage is disabled", file=sys.stderr)
        service.close()
        raise SystemExit(1)
    proactive = ProactiveEngine(service, cfg)
    _COMMANDS[args.command](service, proactive, args)
    service.close()


if __name__ == "__main__":
    main()
