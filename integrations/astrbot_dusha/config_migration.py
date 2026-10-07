"""One-time migration before AstrBot normalizes the plugin configuration schema."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path


def migrate_config(path: Path) -> None:
    if not path.exists():
        return
    path = path.resolve()
    values = json.loads(path.read_text(encoding="utf-8-sig"))
    changed = False
    for old, new in {
        "poll_seconds": "poll_interval_seconds",
        "enable_proactive": "proactive_enabled",
    }.items():
        if old in values:
            values.setdefault(new, values.pop(old))
            changed = True
    if not changed:
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(values, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
