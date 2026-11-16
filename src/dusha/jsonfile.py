from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _write_text(path: Path, text: str, mode: int) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "w", encoding="utf-8") as file:
        file.write(text)


def write_json_atomic(
    path: Path,
    raw: Any,
    *,
    mode: int = 0o600,
    exclusive: bool = False,
    before_replace: Callable[[Path], None] | None = None,
) -> None:
    text = json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
    if exclusive:
        _write_text(path, text, mode)
        return
    temp = path.with_name(f".{path.name}.tmp")
    temp.unlink(missing_ok=True)
    try:
        _write_text(temp, text, mode)
        if before_replace is not None:
            before_replace(temp)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
