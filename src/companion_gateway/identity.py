from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class IdentityError(ValueError):
    pass


def load_identity(identity_config: Any) -> tuple[str, str]:
    path = str(getattr(identity_config, "path", "") or "")
    if not path:
        return "", ""
    source = Path(path)
    if not source.exists():
        raise IdentityError(f"identity prompt file not found: {source}")
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise IdentityError(f"identity prompt file is not valid UTF-8: {source}") from error
    except OSError as error:
        raise IdentityError(f"cannot read identity prompt file {source}: {error}") from error
    revision = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, revision
