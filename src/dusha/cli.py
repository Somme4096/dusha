from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import uvicorn

from .api import create_app
from .config import (
    CONFIG_FILE,
    companion_home,
    config_root,
    create_default_config,
    list_companions,
    load_config,
)
from .interfaces import CommandError, Commands
from .service import CompanionService

_STANDALONE = frozenset({"list", "serve"})


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _globals(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--companion", default=None, help="Companion name under the dusha config root")
    parser.add_argument("--home", default=None, help="Companion home directory")
    parser.add_argument("--config", default=None, help="Configuration file path")


def parser() -> tuple[argparse.ArgumentParser, Commands]:
    root = argparse.ArgumentParser(prog="dusha")
    _globals(root)
    commands = Commands(root)
    commands.command("list", "List companions")
    serve = commands.command("serve", "Run the HTTP service")
    serve.add_argument("serve_companion", nargs="?", default=None, metavar="companion")
    commands.command("health", "Check the database")
    commands.command("backup", "Create a consistent SQLite backup").add_argument("destination")
    commands.command("plugins", "Show plugin states")
    return root, commands


def _core(commands: Commands, service: CompanionService) -> None:
    handlers = {
        "health": lambda args: {"database": service.database.integrity_check()},
        "backup": lambda args: {"backup": service.backup(args.destination)},
        "plugins": lambda args: service.plugins_status(),
    }
    for name, run in handlers.items():
        commands.subparsers.choices[name].set_defaults(run=run)


def _command(argv: list[str]) -> tuple[argparse.Namespace, str | None]:
    early = argparse.ArgumentParser(add_help=False)
    _globals(early)
    known, rest = early.parse_known_args(argv)
    return known, next((item for item in rest if not item.startswith("-")), None)


def _config(args: argparse.Namespace, companion: str | None, serve: bool) -> Any:
    try:
        if serve and args.config is None and (args.home or companion):
            home = Path(args.home).expanduser() if args.home else companion_home(companion)
            if not (home / CONFIG_FILE).exists():
                print(f"created {create_default_config(home)}", file=sys.stderr)
        return load_config(args.config, home=args.home, companion=companion)
    except (FileNotFoundError, ValueError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(2) from error


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    known, command = _command(argv)
    root, commands = parser()
    if command is None or command in _STANDALONE:
        args = root.parse_args(argv)
        if args.command == "list":
            _print({"root": str(config_root()), "companions": list_companions()})
            return
        cfg = _config(args, args.companion or args.serve_companion, serve=True)
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")
        return
    cfg = _config(known, known.companion, serve=False)
    service = CompanionService(cfg, commands)
    _core(commands, service)
    try:
        args = root.parse_args(argv)
        try:
            result = args.run(args)
        except CommandError as error:
            print(error, file=sys.stderr)
            raise SystemExit(1) from error
        _print(result)
    finally:
        service.close()


if __name__ == "__main__":
    main()
