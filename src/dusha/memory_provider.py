from __future__ import annotations

import copy
import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import plugin_runtime as _runtime
from .database import Database
from .hooks import Block, ContextRequest, Contributor, render
from .memory_plugin import (
    BackfillIndexResult,
    IndexStatusResult,
    IngestMessagesRequest,
    IngestMessagesResult,
    InjectContextRequest,
    InjectContextResult,
    MatchPhraseRequest,
    MatchPhraseResult,
    MemoryPluginError,
    RebuildIndexResult,
    RebuildMemoryIndexResult,
)
from .serialization import compact_json
from .timeutil import isoformat, utc_now

logger = logging.getLogger("dusha")
_RESTART_COOLDOWN = 5.0
_RPC_TIMEOUT_ENV = "DUSHA_MEMORY_RPC_TIMEOUT_SECONDS"


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


class _MemoryPluginFailure(MemoryPluginError):
    pass


def resolve_mods_dir(config: Any) -> Path:
    return _runtime.resolve_mods_dir(config, "memory_plugin")


_inside = _runtime.inside


def _suite(module_name: str, mods_dir: Path) -> Path:
    if not _runtime.MODULE_NAME.fullmatch(module_name):
        raise MemoryPluginError(f"memory_plugin.module must contain a plain plugin name, got {module_name!r}")
    root = mods_dir.expanduser().resolve()
    suite = (root / module_name).resolve()
    if not _inside(suite, root) or not suite.is_dir():
        raise MemoryPluginError(f"memory plugin not found: {root / module_name}")
    for name in _runtime.REQUIRED_FILES:
        item = suite / name
        if not item.is_file() or not _inside(item.resolve(), suite):
            raise MemoryPluginError(f"invalid memory plugin: missing or escaping {name}")
    return suite


def _sync(suite: Path, passthrough: Any = ()) -> None:
    venv = suite / ".venv"
    if venv.is_symlink() or (venv.exists() and not _inside(venv.resolve(), suite)):
        raise MemoryPluginError("memory plugin has an escaping .venv")
    env = _runtime.plugin_environment(passthrough)
    env["UV_PROJECT_ENVIRONMENT"] = str(suite / ".venv")
    try:
        result = subprocess.run(
            ["uv", "--no-config", "sync", "--locked", "--no-dev", "--project", str(suite)],
            cwd=suite, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=_runtime.SYNC_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MemoryPluginError(f"memory plugin initialization failed: {exc}") from exc
    if result.returncode or not _python_path(suite).is_file():
        raise MemoryPluginError("memory plugin initialization failed")


def _python_path(suite: Path) -> Path:
    venv = suite / ".venv"
    if venv.is_symlink() or not _inside(venv.resolve(), suite):
        raise MemoryPluginError("memory plugin has an escaping .venv")
    path = suite / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if not path.is_file() or not _inside(path, suite):
        raise MemoryPluginError("memory plugin has an invalid virtual environment")
    return path


def _load(config: Any) -> _SuiteMemory:
    plugin = getattr(config, "memory_plugin", None)
    module = str(getattr(plugin, "module", "") or "").strip()
    if not module:
        raise MemoryPluginError("memory plugin is not configured")
    suite = _suite(module, resolve_mods_dir(config))
    passthrough = tuple(getattr(plugin, "env_passthrough", ()) or ())
    _sync(suite, passthrough)
    return _SuiteMemory(
        suite,
        Path(config.database_path),
        float(plugin.timeout_seconds),
        dict(getattr(plugin, "options", {}) or {}),
        passthrough,
    )


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        proc.kill()


_RESULTS: dict[str, type[Any]] = {
    "ingest_messages": IngestMessagesResult, "inject_context": InjectContextResult,
    "status": IndexStatusResult, "backfill_once": BackfillIndexResult,
    "rebuild_index": RebuildMemoryIndexResult, "rebuild_chunks": RebuildIndexResult,
    "match_phrase": MatchPhraseResult,
}


class _SuiteMemory:
    def __init__(
        self,
        suite: Path,
        database: Path,
        timeout: float,
        options: dict[str, Any],
        passthrough: tuple[str, ...] = (),
    ):
        self.suite, self.database, self.timeout, self.options = suite, database, timeout, options
        self.passthrough = passthrough
        self.proc: subprocess.Popen[bytes] | None = None
        self.responses: queue.Queue[Any] = queue.Queue()
        self.lock = threading.Lock()
        self.next_id = 1
        self.restarts = 0
        self.last_restart = 0.0
        self.start()

    def start(self) -> None:
        try:
            self.proc = subprocess.Popen(
                [str(_python_path(self.suite)), "-I", str(Path(__file__).with_name("memory_worker.py")), str(self.suite), str(self.database)],
                cwd=self.suite, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=os.name != "nt",
                env={**_runtime.plugin_environment(self.passthrough), _RPC_TIMEOUT_ENV: str(self.timeout)},
            )
        except OSError as exc:
            raise MemoryPluginError(f"memory plugin could not start: {exc}") from exc
        assert self.proc.stdout is not None
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        pending = bytearray()
        while True:
            chunk = self.proc.stderr.read(8192)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, _, pending = pending.partition(b"\n")
                self._log_stderr(line)
        self._log_stderr(bytes(pending))

    @staticmethod
    def _log_stderr(raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").strip()
        if text:
            logger.warning("memory plugin stderr: %s", text)

    def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                self.responses.put(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.responses.put(_MemoryPluginFailure("memory plugin returned invalid output"))
        self.responses.put(_MemoryPluginFailure("memory plugin closed its output"))
        self.proc.wait()

    def _restart_locked(self) -> None:
        now = time.monotonic()
        if now - self.last_restart < _RESTART_COOLDOWN:
            raise MemoryPluginError("memory plugin is not running")
        self.last_restart = now
        self.restarts += 1
        logger.warning("memory plugin is not running; restarting (attempt %d)", self.restarts)
        self.responses = queue.Queue()
        self.start()

    def call(self, method: str, request: Any = None) -> Any:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._restart_locked()
            ident = self.next_id
            self.next_id += 1
            params = asdict(request) if is_dataclass(request) else {}
            payload = {"id": ident, "method": method, "params": _json_value({"request": params, "options": copy.deepcopy(self.options)})}
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
                self.proc.stdin.flush()
                response = self.responses.get(timeout=self.timeout)
            except queue.Empty as exc:
                _terminate(self.proc)
                self.proc.wait()
                raise MemoryPluginError("memory plugin timed out") from exc
            except OSError as exc:
                _terminate(self.proc)
                self.proc.wait()
                raise MemoryPluginError(f"memory plugin communication failed: {exc}") from exc
            if isinstance(response, Exception):
                raise response
            if not isinstance(response, dict) or response.get("id") != ident:
                raise MemoryPluginError("memory plugin returned an invalid response")
            if "error" in response:
                error_type = response.get("error_type", "MemoryPluginError")
                error_msg = str(response["error"])
                if error_type not in ("MemoryPluginError", "EvergreenConflict"):
                    error_msg = f"{error_msg} ({error_type})"
                if error_type == "ValueError":
                    raise ValueError(error_msg)
                if error_type == "KeyError":
                    raise KeyError(error_msg)
                if error_type == "EvergreenConflict":
                    from .evergreen import EvergreenConflict
                    raise EvergreenConflict(error_msg)
                raise MemoryPluginError(error_msg)
            return self._result(method, response.get("result"))

    def _result(self, method: str, value: Any) -> Any:
        if method == "close":
            return None
        cls = _RESULTS[method]
        if value is None:
            return None
        if not isinstance(value, dict):
            raise MemoryPluginError("memory plugin returned an invalid result")
        try:
            return cls(**value)
        except (TypeError, KeyError) as exc:
            raise MemoryPluginError("memory plugin returned an invalid result") from exc

    def close(self) -> None:
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.call("close")
        except Exception:
            pass
        if self.proc is not None and self.proc.poll() is None:
            _terminate(self.proc)
        if self.proc is not None:
            self.proc.wait()


_BATCH_MAX_BYTES = 512 * 1024
_CONTEXT_MAX_RECORDS = 50
_CATCH_UP_BATCHES = 20


class MemoryProvider(Contributor):
    def __init__(self, config: Any, database: Database, prompts: dict[str, Any]):
        self.config = config
        self.settings = config.memory_plugin
        self.database = database
        self.blocks = prompts["context_blocks"]
        self.module = str(getattr(self.settings, "module", "") or "").strip()
        self.name = self.module or "memory_plugin"
        self.batch_size = max(1, min(int(self.settings.ingest_batch_size), 500))
        self._lock = threading.Lock()
        self.plugin: _SuiteMemory | None = None
        if not self.module:
            return
        try:
            self.plugin = _load(config)
        except Exception:
            logger.exception("failed to initialize memory plugin")

    @property
    def enabled(self) -> bool:
        return self.plugin is not None

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name not in _RESULTS:
            raise AttributeError(name)
        def operation(request: Any = None, *args: Any, **kwargs: Any) -> Any:
            if self.plugin is None:
                return None
            return self.plugin.call(name, request)
        return operation

    def match_phrase_deltas(self, message: str) -> dict[str, float] | None:
        result = self.match_phrase(MatchPhraseRequest(message=message))
        return result.deltas if result is not None else None

    def on_message(self, message: dict[str, Any]) -> None:
        if self.enabled:
            with self._lock:
                self._push_locked()
        return None

    def on_context(self, request: ContextRequest) -> Block | None:
        cap = int(self.settings.context_max_chars)
        if not self.enabled or cap <= 0:
            return None
        try:
            result = self.inject_context(
                InjectContextRequest(
                    query=request.query,
                    scope=request.conversation_id or request.harness or "",
                    max_chars=cap,
                    harness=request.harness,
                    conversation_id=request.conversation_id,
                )
            )
        except Exception:
            logger.warning("memory plugin context injection failed")
            return None
        data = self._normalize(result, cap)
        if data is None:
            return None
        return render(
            self.name, self.blocks["memory_open_delimiter"], data, self.blocks["memory_close_delimiter"]
        )

    def _normalize(self, result: Any, cap: int) -> dict[str, Any] | None:
        text = result.text if isinstance(getattr(result, "text", None), str) else ""
        text = text[:cap]
        records: list[dict[str, Any]] = []
        remaining = max(0, cap - len(text))
        for item in list(getattr(result, "records", None) or [])[:_CONTEXT_MAX_RECORDS]:
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

    def _cursor(self, db: Any) -> int:
        row = db.execute(
            "SELECT last_acknowledged_id FROM plugin_ingest_state WHERE plugin=?", (self.module,)
        ).fetchone()
        return int(row["last_acknowledged_id"]) if row else 0

    def _batch(self, cursor: int) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT m.id, m.conversation_id, m.role, m.text, m.occurred_at,
                          m.ingested_at, m.sha256, c.harness,
                          c.external_id AS external_conversation_id
                   FROM messages m JOIN conversations c ON c.id=m.conversation_id
                   WHERE m.id > ? AND m.role IN ('user','assistant')
                     AND m.occurred_at > '1970-01-01T00:00:00+00:00'
                   ORDER BY m.id ASC LIMIT ?""",
                (cursor, self.batch_size),
            ).fetchall()
        payload: list[dict[str, Any]] = []
        for row in rows:
            if payload and len(compact_json(payload + [dict(row)])) > _BATCH_MAX_BYTES:
                break
            payload.append(dict(row))
        return payload

    def _push_locked(self) -> dict[str, Any]:
        with self.database.connect() as db:
            cursor = self._cursor(db)
        batch = self._batch(cursor)
        if not batch:
            return {"cursor": cursor, "pushed": 0, "acknowledged": False}
        try:
            result = self.ingest_messages(IngestMessagesRequest(messages=tuple(batch)))
        except Exception:
            logger.warning("memory plugin message ingestion failed", exc_info=True)
            return {"cursor": cursor, "pushed": len(batch), "acknowledged": False}
        highest = getattr(result, "highest_id", None)
        expected = int(batch[-1]["id"])
        if (
            isinstance(highest, bool)
            or not isinstance(highest, int)
            or highest != expected
            or highest <= cursor
        ):
            logger.warning("memory plugin returned an invalid ingestion acknowledgement")
            return {"cursor": cursor, "pushed": len(batch), "acknowledged": False}
        with self.database.connect() as db:
            db.execute(
                """INSERT INTO plugin_ingest_state(plugin, last_acknowledged_id, updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(plugin) DO UPDATE SET
                     last_acknowledged_id=excluded.last_acknowledged_id,
                     updated_at=excluded.updated_at""",
                (self.module, int(highest), isoformat(utc_now())),
            )
        return {"cursor": int(highest), "pushed": len(batch), "acknowledged": True}

    def catch_up(self) -> dict[str, Any]:
        if not self.enabled:
            return {"skipped": True}
        pushed, cursor, acknowledged, pending = 0, 0, False, False
        with self._lock:
            for _ in range(_CATCH_UP_BATCHES):
                result = self._push_locked()
                cursor = int(result.get("cursor", cursor))
                if result.get("acknowledged"):
                    pushed += int(result.get("pushed", 0))
                    acknowledged = True
                    continue
                pending = bool(result.get("pushed"))
                break
        if acknowledged:
            logger.info("memory plugin ingestion acknowledged through id %s", cursor)
        elif pending:
            logger.warning("memory plugin ingestion backlog pending")
        return {"cursor": cursor, "pushed": pushed, "acknowledged": acknowledged, "pending": pending}

    def close(self) -> None:
        if self.plugin is not None:
            self.plugin.close()
