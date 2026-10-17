from __future__ import annotations

import contextlib
import dataclasses
import importlib
import io
import json
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

_OUTPUT_LIMIT = 1_048_576
_ERRORS: dict[str, type[Exception]] = {
    "ValueError": ValueError,
    "KeyError": KeyError,
    "PermissionError": PermissionError,
    "AttributeError": AttributeError,
}


class RemoteError(RuntimeError):
    pass


class _BoundedOutput(io.StringIO):
    def write(self, value: str) -> int:
        if self.tell() + len(value) > _OUTPUT_LIMIT:
            raise RuntimeError("mod output limit exceeded")
        return super().write(value)


class Record(dict):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_value(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    return value


class Channel:
    def __init__(self, reader: Any, writer: Any):
        self.reader = reader
        self.writer = writer
        self.next_call = 1

    def send(self, payload: dict[str, Any]) -> None:
        self.writer.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.writer.flush()

    def receive(self) -> dict[str, Any] | None:
        line = self.reader.readline()
        return json.loads(line) if line else None

    def call(self, service: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        ident = self.next_call
        self.next_call += 1
        self.send({"call": ident, "service": service, "method": method, "args": args, "kwargs": kwargs})
        while True:
            message = self.receive()
            if message is None:
                raise RemoteError("host closed the connection")
            if message.get("reply") != ident:
                continue
            if "error" in message:
                raise _ERRORS.get(str(message.get("error_type")), RemoteError)(str(message["error"]))
            return message.get("result")


class Service:
    def __init__(self, channel: Channel, name: str):
        self._channel = channel
        self._name = name

    def __getattr__(self, method: str) -> Callable[..., Any]:
        if method.startswith("_"):
            raise AttributeError(method)

        def invoke(*args: Any, **kwargs: Any) -> Any:
            return self._channel.call(self._name, method, _json_value(list(args)), _json_value(kwargs))

        return invoke


class Context:
    def __init__(self, channel: Channel, data_dir: str):
        self._channel = channel
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.listeners: dict[str, list[Callable[..., Any]]] = {}
        self.intervals: list[tuple[float, Callable[[], Any]]] = []
        self.disposables: list[Callable[[], Any]] = []

    def on(self, event: str, listener: Callable[..., Any]) -> None:
        self.listeners.setdefault(event, []).append(listener)

    def interval(self, seconds: float, callback: Callable[[], Any]) -> None:
        self.intervals.append((float(seconds), callback))

    def on_dispose(self, callback: Callable[[], Any]) -> None:
        self.disposables.append(callback)

    def __getattr__(self, name: str) -> Service:
        if name.startswith("_"):
            raise AttributeError(name)
        return Service(self._channel, name)


class Worker:
    def __init__(self, suite: Path, channel: Channel):
        self.suite = suite.resolve()
        self.channel = channel
        self.ctx: Context | None = None

    def start(self, entry: str, config: dict[str, Any], data_dir: str) -> dict[str, Any]:
        for path in (self.suite / "src", self.suite):
            if path.is_dir():
                sys.path.insert(0, str(path))
        module = importlib.import_module(entry)
        apply = getattr(module, "apply", None)
        if not callable(apply):
            raise RuntimeError(f"mod entry {entry!r} does not define apply(ctx, config)")
        self.ctx = Context(self.channel, data_dir)
        apply(self.ctx, config)
        intervals = [seconds for seconds, _ in self.ctx.intervals]
        return {"events": sorted(self.ctx.listeners), "intervals": intervals}

    def handle(self, method: str, params: dict[str, Any]) -> Any:
        if method == "start":
            return self.start(str(params["entry"]), dict(params.get("config") or {}), str(params["data_dir"]))
        if self.ctx is None:
            raise RuntimeError("mod is not started")
        if method == "event":
            args = [Record(item) if isinstance(item, dict) else item for item in params.get("args", [])]
            for listener in self.ctx.listeners.get(str(params["event"]), []):
                result = listener(*args)
                if result is not None:
                    return result
            return None
        if method == "interval":
            return self.ctx.intervals[int(params["index"])][1]()
        if method == "dispose":
            for dispose in reversed(self.ctx.disposables):
                dispose()
            return None
        raise ValueError(f"unknown method {method!r}")


def main() -> int:
    if len(sys.argv) != 2:
        raise RuntimeError("usage: mod_worker.py SUITE")
    channel = Channel(sys.stdin, sys.stdout)
    worker = Worker(Path(sys.argv[1]), channel)
    while True:
        request = channel.receive()
        if request is None:
            return 0
        try:
            with contextlib.redirect_stdout(_BoundedOutput()):
                result = worker.handle(str(request["method"]), request.get("params") or {})
            response = {"id": request.get("id"), "result": _json_value(result)}
        except (ValueError, KeyError) as error:
            response = {"id": request.get("id"), "error": str(error), "error_type": type(error).__name__}
        except Exception as error:
            sys.stderr.write(traceback.format_exc())
            sys.stderr.flush()
            response = {
                "id": request.get("id"),
                "error": f"{type(error).__name__}: {error}",
                "error_type": type(error).__name__,
            }
        channel.send(response)


if __name__ == "__main__":
    raise SystemExit(main())
