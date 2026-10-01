from __future__ import annotations

import copy
import json
import logging
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import default_config_dir
from .memory_plugin import (
    BackfillIndexResult,
    IndexStatusResult,
    IngestMessagesResult,
    InjectContextResult,
    MatchPhraseResult,
    MemoryPluginError,
    RebuildIndexResult,
    RebuildMemoryIndexResult,
)

logger = logging.getLogger("companion_gateway")
_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REQUIRED_FILES = ("README.md", "main.py", "pyproject.toml", "uv.lock")
_SYNC_TIMEOUT = 120.0
_RUN_TIMEOUT = 15.0
_RESTART_COOLDOWN = 5.0
_RPC_TIMEOUT_ENV = "SOPHIA_MEMORY_RPC_TIMEOUT_SECONDS"


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
    env = os.getenv("COMPANION_GATEWAY_MODS_DIR", "")
    if env:
        return Path(env).expanduser()
    plugin = getattr(config, "memory_plugin", None)
    configured = str(getattr(plugin, "mods_dir", "") or "")
    return Path(configured).expanduser() if configured else default_config_dir() / "mods"


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _suite(module_name: str, mods_dir: Path) -> Path:
    if not _MODULE_NAME.fullmatch(module_name):
        raise MemoryPluginError(f"memory_plugin.module must contain a plain plugin name, got {module_name!r}")
    root = mods_dir.expanduser().resolve()
    suite = (root / module_name).resolve()
    if not _inside(suite, root) or not suite.is_dir():
        raise MemoryPluginError(f"memory plugin not found: {root / module_name}")
    for name in _REQUIRED_FILES:
        item = suite / name
        if not item.is_file() or not _inside(item.resolve(), suite):
            raise MemoryPluginError(f"invalid memory plugin: missing or escaping {name}")
    return suite


def _sync(suite: Path) -> None:
    venv = suite / ".venv"
    if venv.is_symlink() or (venv.exists() and not _inside(venv.resolve(), suite)):
        raise MemoryPluginError("memory plugin has an escaping .venv")
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["UV_PROJECT_ENVIRONMENT"] = str(suite / ".venv")
    try:
        result = subprocess.run(
            ["uv", "--no-config", "sync", "--locked", "--no-dev", "--project", str(suite)],
            cwd=suite, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=_SYNC_TIMEOUT,
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
    _sync(suite)
    return _SuiteMemory(suite, Path(config.database_path), float(getattr(plugin, "timeout_seconds", _RUN_TIMEOUT)), dict(getattr(plugin, "options", {}) or {}))


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
    def __init__(self, suite: Path, database: Path, timeout: float, options: dict[str, Any]):
        self.suite, self.database, self.timeout, self.options = suite, database, timeout, options
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
                env={**os.environ, "PYTHONNOUSERSITE": "1", _RPC_TIMEOUT_ENV: str(self.timeout)},
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


class MemoryProvider:
    def __init__(self, config: Any):
        self.config = config
        self.plugin: _SuiteMemory | None = None
        try:
            self.plugin = _load(config)
        except Exception:
            logger.exception("failed to initialize memory plugin")

    @property
    def enabled(self) -> bool:
        return self.plugin is not None

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name not in (*_RESULTS, "close"):
            raise AttributeError(name)
        def operation(request: Any = None, *args: Any, **kwargs: Any) -> Any:
            if self.plugin is None:
                return None
            return self.plugin.call(name, request)
        return operation
