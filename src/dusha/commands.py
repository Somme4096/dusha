from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

RESERVED = frozenset({"serve", "proxy", "list", "plugins"})
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_TYPES = {str: "str", int: "int", float: "float", bool: "bool"}
_SCHEMA = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "choice": "string"}


class CommandError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Arg:
    name: str
    type: str = "str"
    help: str = ""
    option: bool = False
    required: bool = True
    default: Any = None
    choices: tuple[str, ...] = ()
    repeated: bool = False

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "help": self.help,
            "option": self.option,
            "required": self.required,
            "default": self.default,
            "choices": list(self.choices),
            "repeated": self.repeated,
        }

    def schema(self) -> dict[str, Any]:
        item: dict[str, Any] = {"type": _SCHEMA[self.type]}
        if self.choices:
            item["enum"] = list(self.choices)
        schema = {"type": "array", "items": item} if self.repeated else item
        if self.help:
            schema["description"] = self.help
        if self.default is not None:
            schema["default"] = self.default
        return schema

    def check(self, value: Any) -> Any:
        if self.repeated:
            if not isinstance(value, list):
                raise ValueError(f"{self.name} must be a list")
            return [self._one(item) for item in value]
        return self._one(value)

    def _one(self, value: Any) -> Any:
        kind = self.type
        if kind == "choice":
            if value not in self.choices:
                raise ValueError(f"{self.name} must be one of {list(self.choices)}")
            return value
        if kind == "bool" and isinstance(value, bool):
            return value
        if kind == "str" and isinstance(value, str):
            return value
        if kind == "int" and isinstance(value, int) and not isinstance(value, bool):
            return value
        if kind == "float" and isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value):
                return float(value)
        raise ValueError(f"{self.name} must be {kind}")


def _kind(type: type, choices: Iterable[str]) -> str:
    if choices:
        return "choice"
    if type not in _TYPES:
        raise ValueError(f"argument type must be one of str, int, float, bool, got {type!r}")
    return _TYPES[type]


def arg(
    name: str,
    type: type = str,
    *,
    help: str = "",
    choices: Iterable[str] = (),
    optional: bool = False,
    repeated: bool = False,
) -> Arg:
    choices = tuple(choices)
    kind = _kind(type, choices)
    if kind == "bool":
        raise ValueError(f"{name} is a bool, make it an option")
    return Arg(name, kind, help, False, not optional, None, choices, repeated)


def option(
    name: str,
    type: type = str,
    *,
    help: str = "",
    default: Any = None,
    choices: Iterable[str] = (),
    required: bool = False,
    repeated: bool = False,
) -> Arg:
    choices = tuple(choices)
    kind = _kind(type, choices)
    if kind == "bool":
        default = bool(default)
    return Arg(name, kind, help, True, required, default, choices, repeated)


def validate(args: Iterable[Arg], values: Any) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("arguments must be a JSON object")
    specs = {item.name: item for item in args}
    unknown = sorted(set(values) - set(specs))
    if unknown:
        raise ValueError(f"unknown argument(s): {unknown}")
    result: dict[str, Any] = {}
    missing: list[str] = []
    for name, spec in specs.items():
        value = values.get(name)
        if value is None or (spec.repeated and value == [] and spec.required):
            if spec.required:
                missing.append(name)
            elif spec.repeated:
                result[name] = list(spec.default or [])
            else:
                result[name] = spec.default
            continue
        result[name] = spec.check(value)
    if missing:
        raise ValueError(f"missing argument(s): {missing}")
    return result


def message(error: Exception) -> str:
    if isinstance(error, KeyError):
        return f"not found: {error.args[0]}" if error.args else "not found"
    return str(error)


@dataclass(frozen=True, slots=True)
class Command:
    path: tuple[str, ...]
    help: str
    args: tuple[Arg, ...]
    handler: Callable[..., Any]

    def spec(self) -> dict[str, Any]:
        group = self.path[0] if len(self.path) > 1 else ""
        args = [item.spec() for item in self.args]
        return {"group": group, "name": self.path[-1], "help": self.help, "args": args}


class Group:
    def __init__(self, commands: Commands, name: str):
        self._commands = commands
        self.name = name

    def command(
        self, name: str, handler: Callable[..., Any], *, help: str = "", args: Iterable[Arg] = ()
    ) -> None:
        self._commands._add((self.name, name), handler, help, tuple(args))


class Commands:
    def __init__(self) -> None:
        self.groups: dict[str, str] = {}
        self.entries: dict[tuple[str, ...], Command] = {}

    def group(self, name: str, help: str = "") -> Group:
        self._top(name)
        self.groups[name] = help
        return Group(self, name)

    def command(
        self, name: str, handler: Callable[..., Any], *, help: str = "", args: Iterable[Arg] = ()
    ) -> None:
        self._top(name)
        self._add((name,), handler, help, tuple(args))

    def find(self, path: tuple[str, ...]) -> Command | None:
        return self.entries.get(path)

    def manifest(self) -> dict[str, Any]:
        used = {path[0] for path in self.entries if len(path) > 1}
        return {
            "groups": {name: help for name, help in self.groups.items() if name in used},
            "commands": [command.spec() for command in self.entries.values()],
        }

    def _top(self, name: str) -> None:
        if not NAME.fullmatch(name):
            raise ValueError(f"command name must be letters, digits, dash, or underscore, got {name!r}")
        if name in RESERVED or name in self.groups or (name,) in self.entries:
            raise ValueError(f"command {name!r} is already taken")

    def _add(
        self, path: tuple[str, ...], handler: Callable[..., Any], help: str, args: tuple[Arg, ...]
    ) -> None:
        label = " ".join(path)
        if not NAME.fullmatch(path[-1]):
            raise ValueError(f"command name must be letters, digits, dash, or underscore, got {path[-1]!r}")
        if path in self.entries:
            raise ValueError(f"command {label!r} is already registered")
        names = [item.name for item in args]
        if len(set(names)) != len(names):
            raise ValueError(f"command {label!r} repeats an argument name")
        for item in args:
            if not item.name.isidentifier() or item.name == "help":
                raise ValueError(f"command {label!r} has an invalid argument name {item.name!r}")
        positional = [item for item in args if not item.option]
        for before, after in zip(positional, positional[1:], strict=False):
            if before.repeated or (after.required and not before.required):
                raise ValueError(f"command {label!r} lists positionals out of order")
        self.entries[path] = Command(path, help, args, handler)
