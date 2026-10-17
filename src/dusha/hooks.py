from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .serialization import safe_json

logger = logging.getLogger("dusha")


@dataclass(frozen=True)
class ContextRequest:
    query: str = ""
    harness: str = ""
    conversation_id: str = ""
    exclude_message_ids: tuple[int, ...] = ()
    include_recent: bool = True


@dataclass(frozen=True)
class DecisionRequest:
    message: str
    emotions: dict[str, dict[str, Any]] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    instruction: str = ""


@dataclass(frozen=True)
class Block:
    name: str
    text: str
    data: Any
    session: tuple[dict[str, Any], ...] = ()


def render(name: str, open_delimiter: str, data: Any, close_delimiter: str) -> Block:
    return Block(name, open_delimiter + safe_json(data) + close_delimiter, data)


@dataclass
class Job:
    plugin: str
    seconds: float
    callback: Callable[[], Any]


class Runtime:
    def __init__(self, plugin: Any, config: Any):
        self.plugin = plugin
        self.name = str(plugin.name)
        self.config = config
        self.inject = tuple(getattr(plugin, "inject", ()) or ())
        self.state = "pending"
        self.error = ""
        self.listeners: dict[str, list[Callable[..., Any]]] = {}
        self.disposables: list[Callable[[], Any]] = []
        self.provides: list[str] = []

    def status(self) -> dict[str, Any]:
        state = "failed" if self.state == "active" and self.error else self.state
        return {
            "state": state,
            "error": self.error,
            "inject": list(self.inject),
            "provides": list(self.provides),
            "events": sorted(self.listeners),
        }


class Context:
    def __init__(self, registry: Registry, runtime: Runtime):
        self._registry = registry
        self._runtime = runtime

    @property
    def name(self) -> str:
        return self._runtime.name

    @property
    def data_dir(self) -> Path:
        path = (self._registry.data_dir / self._runtime.name).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def error(self) -> str:
        return self._runtime.error

    @error.setter
    def error(self, value: str) -> None:
        self._runtime.error = str(value or "")

    def on(self, event: str, listener: Callable[..., Any]) -> None:
        self._runtime.listeners.setdefault(event, []).append(listener)

    def emit(self, event: str, *args: Any) -> None:
        self._registry.emit(event, *args)

    def collect(self, event: str, *args: Any) -> dict[str, Any]:
        return self._registry.collect(event, *args)

    def provide(self, name: str, value: Any) -> None:
        self._registry.provide(self._runtime, name, value)

    def interval(self, seconds: float, callback: Callable[[], Any]) -> None:
        self._registry.jobs.append(Job(self._runtime.name, float(seconds), callback))

    def on_dispose(self, callback: Callable[[], Any]) -> None:
        self._runtime.disposables.append(callback)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._registry.service(name, self._runtime)


class Registry:
    def __init__(self, core: dict[str, Any], data_dir: Path):
        self.core = dict(core)
        self.data_dir = data_dir
        self.services: dict[str, Any] = {}
        self.runtimes: list[Runtime] = []
        self.jobs: list[Job] = []

    def plugin(self, plugin: Any, config: Any) -> Runtime:
        runtime = Runtime(plugin, config)
        self.runtimes.append(runtime)
        return runtime

    def start(self) -> None:
        pending = [runtime for runtime in self.runtimes if runtime.state == "pending"]
        progress = True
        while pending and progress:
            progress = False
            for runtime in list(pending):
                if all(self.available(name) for name in runtime.inject):
                    pending.remove(runtime)
                    self._apply(runtime)
                    progress = True
        for runtime in pending:
            missing = [name for name in runtime.inject if not self.available(name)]
            runtime.state = "inactive"
            runtime.error = f"missing service(s): {', '.join(missing)}"
            logger.warning("%s is inactive, %s", runtime.name, runtime.error)

    def _apply(self, runtime: Runtime) -> None:
        try:
            runtime.plugin.apply(Context(self, runtime), runtime.config)
        except Exception:
            runtime.state = "active"
            self.close()
            raise
        runtime.state = "active"

    def available(self, name: str) -> bool:
        return name in self.core or name in self.services

    def provide(self, runtime: Runtime, name: str, value: Any) -> None:
        if self.available(name):
            raise ValueError(f"service {name!r} is already provided")
        self.services[name] = value
        runtime.provides.append(name)

    def service(self, name: str, runtime: Runtime) -> Any:
        if name in self.core:
            return self.core[name]
        if name not in runtime.inject and name not in runtime.provides:
            raise AttributeError(f"{runtime.name} must inject {name!r} to use it")
        if name not in self.services:
            raise AttributeError(f"service {name!r} is not available")
        return self.services[name]

    def get(self, name: str) -> Any:
        return self.core.get(name, self.services.get(name))

    def emit(self, event: str, *args: Any) -> None:
        self._dispatch(event, args)

    def collect(self, event: str, *args: Any) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for name, value in self._dispatch(event, args):
            if value is not None and name not in results:
                results[name] = value
        return results

    def _dispatch(self, event: str, args: tuple[Any, ...]) -> list[tuple[str, Any]]:
        results: list[tuple[str, Any]] = []
        for runtime in self.runtimes:
            if runtime.state != "active":
                continue
            for listener in list(runtime.listeners.get(event, ())):
                try:
                    results.append((runtime.name, listener(*args)))
                except Exception:
                    logger.exception("%s %s failed", runtime.name, event)
        return results

    def run_jobs(self) -> None:
        for job in list(self.jobs):
            try:
                job.callback()
            except Exception:
                logger.exception("%s interval failed", job.plugin)

    def status(self) -> dict[str, Any]:
        return {runtime.name: runtime.status() for runtime in self.runtimes}

    def close(self) -> None:
        for runtime in reversed(self.runtimes):
            if runtime.state != "active":
                continue
            for dispose in reversed(runtime.disposables):
                try:
                    dispose()
                except Exception:
                    logger.exception("%s failed to close", runtime.name)
            runtime.state = "disposed"
