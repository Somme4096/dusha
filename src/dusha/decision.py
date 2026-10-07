from __future__ import annotations

import copy
import json
import logging
import os
import signal
import threading
import time
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import plugin_runtime as _runtime

logger = logging.getLogger("dusha")

_MAX_OUTPUT = 1_048_576
_STDERR_LOG_LINES = 20


@dataclass(frozen=True)
class DecisionRequest:
    message: str
    emotions: dict[str, dict[str, Any]]
    state: dict[str, Any]
    instruction: str


@dataclass(frozen=True)
class DecisionResult:
    emotion: str


class DecisionPluginError(RuntimeError):
    pass


def resolve_mods_dir(config: Any) -> Path:
    return _runtime.resolve_mods_dir(config, "decision")


_inside = _runtime.inside


def _suite(module_name: str, mods_dir: Path) -> Path:
    if not _runtime.MODULE_NAME.fullmatch(module_name):
        raise DecisionPluginError(
            f"decision.module must contain a plain plugin name, got {module_name!r}"
        )
    root = mods_dir.expanduser().resolve()
    suite = (root / module_name).resolve()
    if not _inside(suite, root) or not suite.is_dir():
        raise DecisionPluginError(f"decision plugin not found: {root / module_name}")
    for name in _runtime.REQUIRED_FILES:
        item = suite / name
        resolved = item.resolve()
        if not item.is_file() or not _inside(resolved, suite):
            raise DecisionPluginError(f"invalid decision plugin: missing or escaping {name}")
    return suite


def _python_path(suite: Path) -> Path:
    if os.name == "nt":
        return suite / ".venv" / "Scripts" / "python.exe"
    return suite / ".venv" / "bin" / "python"


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        proc.kill()


def _run_bounded(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: float, input_data: bytes | None = None
) -> tuple[int, bytes, bytes]:
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        shell=False,
        start_new_session=os.name != "nt",
    )
    streams: list[bytearray] = [bytearray(), bytearray()]
    overflow = threading.Event()

    def write_input() -> None:
        if input_data is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(input_data)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def drain(stream: Any, buffer: bytearray) -> None:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            if len(buffer) <= _MAX_OUTPUT:
                buffer.extend(chunk[: _MAX_OUTPUT + 1 - len(buffer)])
            if len(buffer) > _MAX_OUTPUT:
                overflow.set()

    threads = [
        threading.Thread(target=drain, args=(proc.stdout, streams[0]), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, streams[1]), daemon=True),
    ]
    writer = threading.Thread(target=write_input, daemon=True)
    writer.start()
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if overflow.is_set() or time.monotonic() >= deadline:
            _terminate(proc)
            break
        time.sleep(0.01)
    _terminate(proc)
    remaining = max(0.0, deadline - time.monotonic())
    try:
        returncode = proc.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        _terminate(proc)
        try:
            returncode = proc.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            returncode = -signal.SIGKILL if os.name != "nt" else -1
    for thread in threads + [writer]:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return returncode, bytes(streams[0]), bytes(streams[1])


def _sync(suite: Path, passthrough: Any = ()) -> None:
    environment_path = suite / ".venv"
    if environment_path.exists() or environment_path.is_symlink():
        if environment_path.is_symlink() or not environment_path.is_dir():
            raise DecisionPluginError("decision plugin environment target is invalid")
        if not _inside(environment_path.resolve(), suite):
            raise DecisionPluginError("decision plugin environment escapes plugin")
    environment = _runtime.plugin_environment(passthrough)
    environment["UV_PROJECT_ENVIRONMENT"] = str(environment_path)
    try:
        returncode, _, _ = _run_bounded(
            ["uv", "--no-config", "sync", "--locked", "--no-dev", "--project", str(suite)],
            cwd=suite,
            env=environment,
            timeout=_runtime.SYNC_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise DecisionPluginError(f"decision plugin initialization failed: {exc}") from exc
    if returncode != 0:
        raise DecisionPluginError("decision plugin initialization failed")
    if environment_path.is_symlink() or not _inside(environment_path.resolve(), suite):
        raise DecisionPluginError("decision plugin environment escapes plugin")
    if not _python_path(suite).is_file():
        raise DecisionPluginError("decision plugin environment was not created")


def _load_decider(
    module_name: str, mods_dir: Path, timeout: float, passthrough: Any = ()
) -> Callable[..., Any]:
    suite = _suite(module_name, mods_dir)
    _sync(suite, passthrough)
    return _SuiteDecider(suite, timeout, passthrough)


class _SuiteDecider:
    def __init__(self, suite: Path, timeout: float, passthrough: Any = ()):
        self.passthrough = tuple(passthrough or ())
        self.suite = suite
        self.python = _python_path(suite)
        self.worker = Path(__file__).with_name("decision_worker.py").resolve()
        self.timeout = timeout

    def __call__(self, request: DecisionRequest, options: dict[str, Any]) -> Any:
        payload = json.dumps({
            "request": {
                "message": request.message,
                "emotions": request.emotions,
                "state": request.state,
                "instruction": request.instruction,
            },
            "options": options,
        }, ensure_ascii=False)
        proc: subprocess.Popen[bytes] | None = None
        try:
            returncode, stdout, stderr = _run_bounded(
                [str(self.python), "-I", str(self.worker), str(self.suite)],
                cwd=self.suite,
                env=_runtime.plugin_environment(self.passthrough),
                timeout=self.timeout,
                input_data=payload.encode("utf-8"),
            )
        except OSError as exc:
            raise DecisionPluginError(f"decision plugin could not start: {exc}") from exc
        if len(stdout) > _MAX_OUTPUT or len(stderr) > _MAX_OUTPUT or returncode != 0:
            for line in stderr.decode("utf-8", "replace").strip().splitlines()[-_STDERR_LOG_LINES:]:
                logger.warning("decision plugin stderr: %s", line)
            raise DecisionPluginError("decision plugin failed")
        try:
            result = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DecisionPluginError("decision plugin returned invalid output") from exc
        if result is None:
            return None
        if not isinstance(result, dict) or set(result) != {"emotion"} or not isinstance(result["emotion"], str):
            raise DecisionPluginError("decision plugin returned an invalid result")
        return DecisionResult(result["emotion"])


def load_decider(config: Any) -> Callable[..., Any] | None:
    decision = getattr(config, "decision", None)
    module_name = str(getattr(decision, "module", "") or "").strip()
    if not module_name:
        return None
    try:
        return _load_decider(
            module_name,
            resolve_mods_dir(config),
            float(decision.timeout_seconds),
            getattr(decision, "env_passthrough", ()),
        )
    except Exception:
        logger.exception("failed to initialize decision plugin %r", module_name)
        return None


class DecisionProvider:
    def __init__(self, config: Any):
        self.config = config
        self.decide = load_decider(config)
        decision = getattr(config, "decision", None)
        self.options = dict(getattr(decision, "options", {}) or {})

    @property
    def enabled(self) -> bool:
        return self.decide is not None

    def evaluate(self, *, message: str, emotions: dict[str, dict[str, Any]], state: dict[str, Any], instruction: str) -> str | None:
        if self.decide is None:
            return None
        request = DecisionRequest(str(message), copy.deepcopy(emotions), copy.deepcopy(state), str(instruction))
        try:
            result = self.decide(request, copy.deepcopy(self.options))
            emotion = getattr(result, "emotion", None) if result is not None else None
        except Exception:
            logger.exception("decision plugin failed. No decision-driven adjustment")
            return None
        if not isinstance(emotion, str) or emotion not in emotions:
            if emotion is not None:
                logger.warning("decision plugin returned invalid emotion: %r", emotion)
            return None
        return emotion
