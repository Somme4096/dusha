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
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .database import Database
from .hooks import Block, ContextRequest, Contributor, DecisionRequest, render
from .memory import MemoryStore
from .timeutil import isoformat, utc_now

logger = logging.getLogger("dusha")

MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REQUIRED_FILES = ("README.md", "main.py", "pyproject.toml", "uv.lock")
MODS_DIR_ENV = "DUSHA_MODS_DIR"
TIMEOUT_ENV = "DUSHA_MOD_TIMEOUT_SECONDS"
_BASE_ENV = frozenset({"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT"})
_SYNC_TIMEOUT = 120.0
_RESTART_COOLDOWN = 5.0
_DELIVERY_LIMIT = 500
_CONTEXT_MAX_RECORDS = 50


class ModError(RuntimeError):
    pass


def resolve_mods_dir(config: Any) -> Path:
    env = os.getenv(MODS_DIR_ENV, "")
    if env:
        return Path(env).expanduser()
    configured = str(getattr(getattr(config, "mods", None), "dir", "") or "")
    if configured:
        return Path(configured).expanduser()
    return (getattr(config, "home", None) or Path.cwd()) / "mods"


def environment(passthrough: Any = ()) -> dict[str, str]:
    allowed = _BASE_ENV | {str(name) for name in passthrough or ()}
    result = {key: value for key, value in os.environ.items() if key in allowed}
    result["PYTHONNOUSERSITE"] = "1"
    return result


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _suite(name: str, mods_dir: Path) -> Path:
    if not MODULE_NAME.fullmatch(name):
        raise ModError(f"mod name must be a plain directory name, got {name!r}")
    root = mods_dir.expanduser().resolve()
    suite = (root / name).resolve()
    if not _inside(suite, root) or not suite.is_dir():
        raise ModError(f"mod not found: {root / name}")
    for item in REQUIRED_FILES:
        path = suite / item
        if not path.is_file() or not _inside(path.resolve(), suite):
            raise ModError(f"invalid mod: missing or escaping {item}")
    return suite


def _python_path(suite: Path) -> Path:
    venv = suite / ".venv"
    if venv.is_symlink() or (venv.exists() and not _inside(venv.resolve(), suite)):
        raise ModError("mod has an escaping .venv")
    path = suite / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if not path.is_file():
        raise ModError("mod has an invalid virtual environment")
    return path


def _sync(suite: Path, passthrough: Any) -> Path:
    venv = suite / ".venv"
    if venv.is_symlink() or (venv.exists() and not _inside(venv.resolve(), suite)):
        raise ModError("mod has an escaping .venv")
    env = environment(passthrough)
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    try:
        result = subprocess.run(
            ["uv", "--no-config", "sync", "--locked", "--no-dev", "--project", str(suite)],
            cwd=suite, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=_SYNC_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ModError(f"mod environment sync failed: {exc}") from exc
    if result.returncode:
        raise ModError("mod environment sync failed")
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


class Mod(Contributor):
    def __init__(self, name: str, settings: Any, mods_dir: Path, database: Database, prompts: dict[str, Any]):
        self.name = name
        self.settings = settings
        self.mods_dir = mods_dir
        self.database = database
        self.store = MemoryStore(database)
        self.blocks = prompts["context_blocks"]
        self.timeout = float(settings.timeout_seconds)
        self.passthrough = tuple(settings.env_passthrough)
        self.options = {**dict(settings.options), "context_max_chars": int(settings.context_max_chars)}
        self.exports: frozenset[str] = frozenset()
        self.error = ""
        self.suite: Path | None = None
        self.python: Path | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self.responses: queue.Queue[Any] = queue.Queue()
        self.next_id = 1
        self.last_restart = 0.0
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        try:
            self.suite = _suite(self.name, self.mods_dir)
            self.python = _sync(self.suite, self.passthrough)
            with self._lock:
                self._spawn_locked()
        except ModError as error:
            self.error = str(error)
            logger.error("failed to load mod %r: %s", self.name, error)

    def _spawn_locked(self) -> None:
        assert self.suite is not None and self.python is not None
        worker = Path(__file__).with_name("mod_worker.py")
        env = {**environment(self.passthrough), TIMEOUT_ENV: str(self.timeout)}
        try:
            self.proc = subprocess.Popen(
                [str(self.python), "-I", str(worker), str(self.suite), str(self.database.path)],
                cwd=self.suite, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=os.name != "nt", env=env,
            )
        except OSError as exc:
            raise ModError(f"mod could not start: {exc}") from exc
        self.responses = queue.Queue()
        threading.Thread(target=self._read, args=(self.proc, self.responses), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
        try:
            handshake = self._call_locked("start", {"options": copy.deepcopy(self.options)})
            exports = handshake.get("exports") if isinstance(handshake, dict) else None
            if not isinstance(exports, list):
                raise ModError("mod returned an invalid start response")
        except ModError:
            if self.proc.poll() is None:
                _terminate(self.proc)
            raise
        self.exports = frozenset(str(item) for item in exports)
        self.error = ""

    @staticmethod
    def _read(proc: subprocess.Popen[bytes], responses: queue.Queue[Any]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                responses.put(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                responses.put(ModError("mod returned invalid output"))
        responses.put(ModError("mod closed its output"))

    def _drain_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                logger.warning("%s stderr: %s", self.name, text)

    def _restart_locked(self) -> None:
        now = time.monotonic()
        if self.suite is None or now - self.last_restart < _RESTART_COOLDOWN:
            raise ModError("mod is not running")
        self.last_restart = now
        logger.warning("%s is not running, restarting", self.name)
        self._spawn_locked()

    def _call_locked(self, method: str, params: dict[str, Any]) -> Any:
        if self.proc is None or self.proc.poll() is not None:
            self._restart_locked()
        assert self.proc is not None and self.proc.stdin is not None
        ident = self.next_id
        self.next_id += 1
        payload = {"id": ident, "method": method, "params": params}
        try:
            self.proc.stdin.write((json.dumps(payload, ensure_ascii=False, default=str) + "\n").encode())
            self.proc.stdin.flush()
            response = self.responses.get(timeout=self.timeout)
        except queue.Empty as exc:
            _terminate(self.proc)
            raise ModError("mod timed out") from exc
        except OSError as exc:
            _terminate(self.proc)
            raise ModError(f"mod communication failed: {exc}") from exc
        if isinstance(response, Exception):
            raise response
        if not isinstance(response, dict) or response.get("id") != ident:
            raise ModError("mod returned an invalid response")
        if "error" in response:
            raise ModError(str(response["error"]))
        return response.get("result")

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method not in self.exports:
            return None
        with self._lock:
            return self._call_locked(method, params or {})

    def on_message(self, message: dict[str, Any]) -> None:
        if "on_message" in self.exports:
            with self._lock:
                self._deliver_locked()
        return None

    def _cursor(self) -> int:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT last_delivered_id FROM mod_delivery_state WHERE mod=?", (self.name,)
            ).fetchone()
        return int(row["last_delivered_id"]) if row else 0

    def _deliver_locked(self) -> dict[str, Any]:
        cursor = self._cursor()
        delivered = 0
        for message in self.store.after(cursor, _DELIVERY_LIMIT):
            try:
                self._call_locked("on_message", {"message": message})
            except ModError as error:
                logger.warning("%s on_message failed at id %s: %s", self.name, message["id"], error)
                break
            cursor = int(message["id"])
            delivered += 1
            with self.database.connect() as db:
                db.execute(
                    """INSERT INTO mod_delivery_state(mod, last_delivered_id, updated_at)
                       VALUES(?,?,?)
                       ON CONFLICT(mod) DO UPDATE SET
                         last_delivered_id=excluded.last_delivered_id,
                         updated_at=excluded.updated_at""",
                    (self.name, cursor, isoformat(utc_now())),
                )
        return {"cursor": cursor, "delivered": delivered}

    def on_context(self, request: ContextRequest) -> Block | None:
        cap = int(self.settings.context_max_chars)
        if "on_context" not in self.exports or cap <= 0:
            return None
        try:
            result = self.call("on_context", {"request": asdict(request)})
        except ModError as error:
            logger.warning("%s on_context failed: %s", self.name, error)
            return None
        data = self._normalize(result, cap)
        if data is None:
            return None
        return render(self.name, self.blocks["mod_open_delimiter"], data, self.blocks["mod_close_delimiter"])

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
        if "decide" not in self.exports:
            return None
        try:
            result = self.call("decide", {"request": asdict(request)})
        except ModError as error:
            logger.warning("%s decide failed: %s", self.name, error)
            return None
        return result if isinstance(result, dict) else None

    def tick(self) -> dict[str, Any]:
        if not self.exports:
            return {"skipped": True}
        report: dict[str, Any] = {}
        with self._lock:
            if "on_message" in self.exports:
                report = self._deliver_locked()
            if "tick" in self.exports:
                try:
                    report["tick"] = self._call_locked("tick", {})
                except ModError as error:
                    logger.warning("%s tick failed: %s", self.name, error)
        return report

    def status(self) -> dict[str, Any]:
        with self._lock:
            report: dict[str, Any] = {
                "loaded": self.loaded,
                "exports": sorted(self.exports),
                "error": self.error,
                "status": None,
            }
            if report["loaded"] and "status" in self.exports:
                try:
                    report["status"] = self._call_locked("status", {})
                except ModError as error:
                    report["error"] = str(error)
                    report["loaded"] = self.loaded
        return report

    def close(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                if "close" in self.exports:
                    try:
                        self._call_locked("close", {})
                    except ModError:
                        pass
                if self.proc.poll() is None:
                    _terminate(self.proc)
