from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace


class _BoundedOutput(io.StringIO):
    def write(self, value: str) -> int:
        if self.tell() + len(value) > 1_048_576:
            raise RuntimeError("decision suite output limit exceeded")
        return super().write(value)


def main() -> int:
    suite = Path(sys.argv[1]).resolve()
    payload = json.load(sys.stdin)
    request = SimpleNamespace(**payload["request"])
    with contextlib.redirect_stdout(_BoundedOutput()):
        sys.path.insert(0, str(suite))
        namespace: dict[str, object] = {"__name__": "__main__", "__file__": str(suite / "main.py")}
        exec((suite / "main.py").read_text(encoding="utf-8"), namespace)
        decide = namespace.get("decide")
        if not callable(decide):
            raise RuntimeError("main.py does not export decide")
        result = decide(request, payload["options"])
    if result is None:
        print("null")
    elif isinstance(result, dict):
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps({"emotion": getattr(result, "emotion", None)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
