from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class _BoundedOutput(io.StringIO):
    def write(self, value: str) -> int:
        if self.tell() + len(value) > 1_048_576:
            raise RuntimeError("memory plugin output limit exceeded")
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


def _load(suite: Path) -> dict[str, Any]:
    suite = suite.resolve()
    if not suite.is_dir() or not (suite / "main.py").is_file():
        raise RuntimeError("invalid memory plugin suite")
    sys.path.insert(0, str(suite))
    namespace: dict[str, Any] = {"__name__": "companion_memory_plugin", "__file__": str(suite / "main.py")}
    with contextlib.redirect_stdout(_BoundedOutput()):
        exec(compile((suite / "main.py").read_text(encoding="utf-8"), str(suite / "main.py"), "exec"), namespace)
    return namespace


def main() -> int:
    if len(sys.argv) != 3:
        raise RuntimeError("usage: memory_worker.py SUITE DATABASE")
    suite = Path(sys.argv[1]).resolve()
    database = str(Path(sys.argv[2]).expanduser())
    namespace: dict[str, Any] | None = None
    for line in sys.stdin:
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            if namespace is None:
                namespace = _load(suite)
            method = request["method"]
            function = namespace.get(method)
            if not callable(function):
                raise ValueError(f"memory plugin does not export {method}")
            params = request.get("params") or {}
            values = params.get("request")
            options = dict(params.get("options") or {})
            options["database_path"] = database
            request_value = None if values is None else SimpleNamespace(**values)
            with contextlib.redirect_stdout(_BoundedOutput()):
                result = function(request_value, options)
            response = {"id": request.get("id"), "result": _json_value(result)}
        except (ValueError, KeyError) as error:
            response = {"id": request.get("id"), "error": str(error), "error_type": type(error).__name__}
        except Exception as error:
            response = {"id": request.get("id"), "error": "memory plugin failed", "error_type": type(error).__name__}
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
