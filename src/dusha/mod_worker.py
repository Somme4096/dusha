from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HOOKS = ("start", "on_message", "on_context", "decide", "tick", "status", "close")
_REQUEST_HOOKS = {"on_context", "decide"}
_OUTPUT_LIMIT = 1_048_576


class _BoundedOutput(io.StringIO):
    def write(self, value: str) -> int:
        if self.tell() + len(value) > _OUTPUT_LIMIT:
            raise RuntimeError("mod output limit exceeded")
        return super().write(value)


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: _json_value(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    if hasattr(value, "__dict__"):
        return _json_value(vars(value))
    return value


class _Mod:
    def __init__(self, suite: Path, database: str):
        self.suite = suite.resolve()
        self.database = database
        self.namespace: dict[str, Any] = {}
        self.options: dict[str, Any] = {}

    def start(self, options: dict[str, Any]) -> dict[str, Any]:
        if not self.suite.is_dir() or not (self.suite / "main.py").is_file():
            raise RuntimeError("invalid mod directory")
        sys.path.insert(0, str(self.suite))
        source = self.suite / "main.py"
        self.namespace = {"__name__": "dusha_mod", "__file__": str(source)}
        exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), self.namespace)
        self.options = {**options, "database_path": self.database}
        if callable(self.namespace.get("start")):
            self.namespace["start"](self.options)
        return {"exports": [name for name in HOOKS if callable(self.namespace.get(name))]}

    def call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "start":
            return self.start(dict(params.get("options") or {}))
        function = self.namespace.get(method)
        if method not in HOOKS or not callable(function):
            raise ValueError(f"mod does not export {method}")
        if method == "on_message":
            return function(params["message"], self.options)
        if method in _REQUEST_HOOKS:
            return function(SimpleNamespace(**params["request"]), self.options)
        return function(self.options)


def main() -> int:
    if len(sys.argv) != 3:
        raise RuntimeError("usage: mod_worker.py SUITE DATABASE")
    mod = _Mod(Path(sys.argv[1]), str(Path(sys.argv[2]).expanduser()))
    for line in sys.stdin:
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(_BoundedOutput()):
                result = mod.call(request["method"], request.get("params") or {})
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
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
