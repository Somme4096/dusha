from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
import tomllib
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import PLUGIN_NAME, AppConfig, ExternalPluginConfig
from .hooks import Block, ContextRequest, DecisionRequest, render
from .timeutil import isoformat, utc_now

logger = logging.getLogger("dusha")

REQUIRED_FILES = ("pyproject.toml", "uv.lock")
PLUGINS_DIR_ENV = "DUSHA_PLUGINS_DIR"
TIMEOUT_ENV = "DUSHA_PLUGIN_TIMEOUT_SECONDS"
_BASE_ENV = frozenset({"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT"})
_SYNC_TIMEOUT = 120.0
_RESTART_COOLDOWN = 5.0
_DELIVERY_SECONDS = 10.0
_DELIVERY_LIMIT = 500
_CONTEXT_MAX_RECORDS = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS plugin_delivery_state (
    plugin TEXT PRIMARY KEY,
    last_delivered_id INTEGER NOT NULL DEFAULT 0 CHECK(last_delivered_id >= 0),
    updated_at TEXT NOT NULL
);
"""


class ExternalPluginError(RuntimeError):
    pass


def resolve_plugins_dir(config: AppConfig) -> Path:
    env = os.getenv(PLUGINS_DIR_ENV, "")
    if env:
        return Path(env).expanduser()
    if config.plugins_dir:
        return Path(config.plugins_dir).expanduser()
    return (config.home or Path.cwd()) / "plugins"


def environment(passthrough: Any = ()) -> dict[str, str]:
    allowed = _BASE_ENV | {str(name) for name in passthrough or ()}
    result = {key: value for key, value in os.environ.items() if key in allowed}
    result["PYTHONNOUSERSITE"] = "1"
    return result


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _suite(name: str, plugins_dir: Path) -> Path:
    if not PLUGIN_NAME.fullmatch(name):
        raise ExternalPluginError(f"plugin name must be a plain directory name, got {name!r}")
    root = plugins_dir.expanduser().resolve()
    suite = (root / name).resolve()
    if not _inside(suite, root) or not suite.is_dir():
        raise ExternalPluginError(f"plugin not found: {root / name}")
    for item in REQUIRED_FILES:
        path = suite / item
        if not path.is_file() or not _inside(path.resolve(), suite):
            raise ExternalPluginError(f"invalid plugin: missing or escaping {item}")
    return suite


def _manifest(suite: Path) -> tuple[str, tuple[str, ...]]:
    try:
        project = tomllib.loads((suite / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ExternalPluginError(f"invalid plugin manifest: {error}") from error
    section = project.get("tool", {}).get("dusha")
    if not isinstance(section, dict):
        raise ExternalPluginError("invalid plugin manifest: missing [tool.dusha]")
    unknown = sorted(set(section) - {"entry", "inject"})
    if unknown:
        raise ExternalPluginError(f"invalid plugin manifest: unknown field(s) under [tool.dusha]: {unknown}")
    entry = section.get("entry")
    if not isinstance(entry, str) or not all(part.isidentifier() for part in entry.split(".")):
        raise ExternalPluginError("invalid plugin manifest: tool.dusha.entry must be a module name")
    inject = section.get("inject", [])
    if not isinstance(inject, list) or not all(isinstance(item, str) and item for item in inject):
        raise ExternalPluginError(
            "invalid plugin manifest: tool.dusha.inject must be an array of service names"
        )
    return entry, tuple(inject)


def _python_path(suite: Path) -> Path:
    venv = suite / ".venv"
    if venv.is_symlink() or (venv.exists() and not _inside(venv.resolve(), suite)):
        raise ExternalPluginError("plugin has an escaping .venv")
    path = suite / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if not path.is_file():
        raise ExternalPluginError("plugin has an invalid virtual environment")
    return path


def _sync(suite: Path, passthrough: Any) -> Path:
    venv = suite / ".venv"
    if venv.is_symlink() or (venv.exists() and not _inside(venv.resolve(), suite)):
        raise ExternalPluginError("plugin has an escaping .venv")
    env = environment(passthrough)
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    try:
        result = subprocess.run(
            ["uv", "--no-config", "sync", "--locked", "--no-dev", "--project", str(suite)],
            cwd=suite, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=_SYNC_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExternalPluginError(f"plugin environment sync failed: {exc}") from exc
    if result.returncode:
        raise ExternalPluginError("plugin environment sync failed")
    return _python_path(suite)


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        proc.kill()
    proc.wait()


class RemoteService:
    def __init__(self, plugin: ExternalPlugin, name: str):
        self._plugin = plugin
        self._name = name

    def __getattr__(self, method: str) -> Any:
        if method.startswith("_"):
            raise AttributeError(method)

        def invoke(*args: Any, **kwargs: Any) -> Any:
            return self._plugin.call(
                "provide_call",
                {"service": self._name, "method": method, "args": list(args), "kwargs": kwargs},
            )

        return invoke


class ExternalPlugin:
    def __init__(
        self,
        name: str,
        settings: ExternalPluginConfig,
        plugins_dir: Path,
        database: Any,
        messages: Any,
        prompts: dict[str, Any],
    ):
        self.name = name
        self.settings = settings
        self.database = database
        self.messages = messages
        self.prompts = prompts
        self.timeout = float(settings.timeout_seconds)
        self.passthrough = tuple(settings.env_passthrough)
        self.config = {**dict(settings.options), "context_max_chars": int(settings.context_max_chars)}
        self.suite: Path | None = None
        self.entry = ""
        self.inject: tuple[str, ...] = ()
        self.problem = ""
        try:
            self.suite = _suite(name, plugins_dir)
            self.entry, self.inject = _manifest(self.suite)
        except ExternalPluginError as error:
            self.problem = str(error)
        self.ctx: Any = None
        self.python: Path | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self.reader: threading.Thread | None = None
        self.responses: queue.Queue[Any] = queue.Queue()
        self.exports: frozenset[str] = frozenset()
        self.intervals: list[float] = []
        self.registered: set[str] = set()
        self.next_id = 1
        self.last_restart = 0.0
        self.closing = False
        self.started = False
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    def apply(self, ctx: Any, config: Any) -> None:
        self.ctx = ctx
        if self.problem:
            ctx.error = self.problem
            logger.error("failed to load plugin %r: %s", self.name, self.problem)
            return
        self.database.migrate(SCHEMA)
        ctx.interval(_DELIVERY_SECONDS, self.deliver)
        ctx.on_dispose(self.close)
        try:
            self.python = _sync(self.suite, self.passthrough)
            with self._lock:
                self._spawn_locked()
        except ExternalPluginError as error:
            ctx.error = str(error)
            logger.error("failed to load plugin %r: %s", self.name, error)

    def _spawn_locked(self) -> None:
        assert self.suite is not None and self.python is not None
        worker = Path(__file__).with_name("external_worker.py")
        env = {**environment(self.passthrough), TIMEOUT_ENV: str(self.timeout)}
        try:
            self.proc = subprocess.Popen(
                [str(self.python), "-I", str(worker), str(self.suite)],
                cwd=self.suite, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=os.name != "nt", env=env,
            )
        except OSError as exc:
            raise ExternalPluginError(f"plugin could not start: {exc}") from exc
        self.responses = queue.Queue()
        self.reader = threading.Thread(target=self._read, args=(self.proc, self.responses), daemon=True)
        self.reader.start()
        threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
        self.ctx.error = ""
        try:
            handshake = self._call_locked(
                "start",
                {
                    "entry": self.entry,
                    "name": self.name,
                    "config": self.config,
                    "data_dir": str(self.ctx.data_dir),
                },
            )
            events = handshake.get("events") if isinstance(handshake, dict) else None
            intervals = handshake.get("intervals") if isinstance(handshake, dict) else None
            provides = handshake.get("provides") if isinstance(handshake, dict) else None
            if not isinstance(events, list) or not isinstance(intervals, list):
                raise ExternalPluginError("plugin returned an invalid start response")
            if not all(isinstance(item, str) and item for item in events):
                raise ExternalPluginError("plugin returned an invalid start response")
            if not isinstance(provides, list) or not all(
                isinstance(item, str) and PLUGIN_NAME.fullmatch(item) for item in provides
            ):
                raise ExternalPluginError("plugin returned an invalid start response")
            for name in provides:
                if name in self.ctx.provided:
                    continue
                try:
                    self.ctx.provide(name, RemoteService(self, name))
                except ValueError as error:
                    raise ExternalPluginError(str(error)) from error
        except ExternalPluginError:
            if self.proc.poll() is None:
                _terminate(self.proc)
            raise
        self.exports = frozenset(events)
        self.intervals = [float(item) for item in intervals]
        self.started = True
        self._register()

    def _register(self) -> None:
        handlers = {"message": self.on_message, "context": self.on_context, "decide": self.decide}
        for event in sorted(self.exports - self.registered):
            handler = handlers.get(event) or (lambda *args, event=event: self._event(event, *args))
            self.ctx.on(event, handler)
            self.registered.add(event)
        if "intervals" not in self.registered:
            for index, seconds in enumerate(self.intervals):
                self.ctx.interval(seconds, lambda index=index: self.call("interval", {"index": index}))
            self.registered.add("intervals")

    def _read(self, proc: subprocess.Popen[bytes], responses: queue.Queue[Any]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                responses.put(ExternalPluginError("plugin returned invalid output"))
                continue
            if isinstance(message, dict) and "host" in message:
                self._host(message)
            elif isinstance(message, dict) and "call" in message:
                self._serve(message)
            else:
                responses.put(message)
        responses.put(ExternalPluginError("plugin closed its output"))
        if proc is self.proc and not self.closing:
            self.ctx.error = "plugin closed its output"

    def _host(self, message: dict[str, Any]) -> None:
        method = str(message.get("method"))
        ident = message.get("host")
        params = message.get("params") or {}
        if method == "error":
            self.ctx.error = str(params.get("text", ""))
            return
        reply: dict[str, Any] = {"reply": ident}
        try:
            event = str(params.get("event"))
            args = list(params.get("args") or [])
            if method == "emit":
                self.ctx.emit(event, *args)
                reply["result"] = None
            elif method == "collect":
                reply["result"] = _json_value(self.ctx.collect(event, *args))
            else:
                raise ValueError(f"unknown method {method!r}")
        except Exception as error:
            reply["error"] = str(error)
            reply["error_type"] = type(error).__name__
        try:
            self._write(reply)
        except OSError:
            pass

    def _serve(self, message: dict[str, Any]) -> None:
        reply: dict[str, Any] = {"reply": message.get("call")}
        try:
            service, method = str(message.get("service")), str(message.get("method"))
            if service not in self.inject:
                raise PermissionError(f"{self.name} must inject {service!r} to use it")
            if method.startswith("_"):
                raise AttributeError(f"{service}.{method} is not callable")
            target = getattr(getattr(self.ctx, service), method)
            if not callable(target):
                raise AttributeError(f"{service}.{method} is not callable")
            reply["result"] = _json_value(target(*message.get("args", []), **message.get("kwargs", {})))
        except Exception as error:
            reply["error"] = str(error)
            reply["error_type"] = type(error).__name__
        try:
            self._write(reply)
        except OSError:
            pass

    def _write(self, payload: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        data = (json.dumps(payload, ensure_ascii=False, default=str) + "\n").encode()
        with self._write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _drain_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                logger.warning("%s stderr: %s", self.name, text)

    def _restart_locked(self) -> None:
        now = time.monotonic()
        if not self.started or now - self.last_restart < _RESTART_COOLDOWN:
            raise ExternalPluginError("plugin is not running")
        self.last_restart = now
        logger.warning("%s is not running, restarting", self.name)
        try:
            self._spawn_locked()
        except ExternalPluginError as error:
            self.ctx.error = str(error)
            raise

    def _call_locked(self, method: str, params: dict[str, Any]) -> Any:
        if method != "start" and (self.proc is None or self.proc.poll() is not None):
            self._restart_locked()
        assert self.proc is not None
        ident = self.next_id
        self.next_id += 1
        try:
            self._write({"id": ident, "method": method, "params": _json_value(params)})
            response = self.responses.get(timeout=self.timeout)
        except queue.Empty as exc:
            _terminate(self.proc)
            raise ExternalPluginError("plugin timed out") from exc
        except OSError as exc:
            _terminate(self.proc)
            raise ExternalPluginError(f"plugin communication failed: {exc}") from exc
        if isinstance(response, Exception):
            raise response
        if not isinstance(response, dict) or response.get("id") != ident:
            raise ExternalPluginError("plugin returned an invalid response")
        if "error" in response:
            raise ExternalPluginError(str(response["error"]))
        return response.get("result")

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            return self._call_locked(method, params or {})

    def _reentrant(self) -> bool:
        return threading.current_thread() is self.reader

    def _event(self, event: str, *args: Any) -> Any:
        if event not in self.exports or self._reentrant():
            return None
        try:
            return self.call("event", {"event": event, "args": list(args)})
        except ExternalPluginError as error:
            logger.warning("%s %s failed: %s", self.name, event, error)
            return None

    def on_message(self, message: dict[str, Any]) -> None:
        if "message" not in self.exports or self._reentrant() or not self._lock.acquire(blocking=False):
            return None
        try:
            self._deliver_locked()
        finally:
            self._lock.release()
        return None

    def deliver(self) -> dict[str, Any]:
        with self._lock:
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._restart_locked()
            except ExternalPluginError as error:
                return {"error": str(error)}
            if "message" not in self.exports:
                return {"skipped": True}
            return self._deliver_locked()

    def _cursor(self) -> int:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT last_delivered_id FROM plugin_delivery_state WHERE plugin=?", (self.name,)
            ).fetchone()
        return int(row["last_delivered_id"]) if row else 0

    def _deliver_locked(self) -> dict[str, Any]:
        cursor = self._cursor()
        delivered = 0
        for message in self.messages.after(cursor, _DELIVERY_LIMIT):
            try:
                self._call_locked("event", {"event": "message", "args": [message]})
            except ExternalPluginError as error:
                logger.warning("%s message failed at id %s: %s", self.name, message["id"], error)
                break
            cursor = int(message["id"])
            delivered += 1
            with self.database.connect() as db:
                db.execute(
                    """INSERT INTO plugin_delivery_state(plugin, last_delivered_id, updated_at)
                       VALUES(?,?,?)
                       ON CONFLICT(plugin) DO UPDATE SET
                         last_delivered_id=excluded.last_delivered_id,
                         updated_at=excluded.updated_at""",
                    (self.name, cursor, isoformat(utc_now())),
                )
        return {"cursor": cursor, "delivered": delivered}

    def on_context(self, request: ContextRequest) -> Block | None:
        cap = int(self.settings.context_max_chars)
        if cap <= 0:
            return None
        data = self._normalize(self._event("context", request), cap)
        if data is None:
            return None
        blocks = self.prompts["context_blocks"]
        return render(self.name, blocks["plugin_open_delimiter"], data, blocks["plugin_close_delimiter"])

    def _normalize(self, result: Any, cap: int) -> dict[str, Any] | None:
        if not isinstance(result, dict):
            return None
        text = result.get("text") if isinstance(result.get("text"), str) else ""
        text = text[:cap]
        remaining = max(0, cap - len(text))
        records: list[dict[str, Any]] = []
        for item in list(result.get("records") or [])[:_CONTEXT_MAX_RECORDS]:
            if not isinstance(item, dict):
                continue
            source, body = item.get("source"), item.get("text")
            if not isinstance(source, str) or not isinstance(body, str):
                continue
            body = body[:remaining]
            if not body:
                break
            records.append({"source": source[:200], "text": body})
            remaining -= len(body)
        if not text and not records:
            return None
        data: dict[str, Any] = {"source": self.name, "text": text}
        if records:
            data["records"] = records
        return data

    def decide(self, request: DecisionRequest) -> dict[str, Any] | None:
        result = self._event("decide", request)
        return result if isinstance(result, dict) else None

    def close(self) -> None:
        self.closing = True
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                try:
                    self._call_locked("dispose", {})
                except ExternalPluginError:
                    pass
                if self.proc.poll() is None:
                    _terminate(self.proc)
