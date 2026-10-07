from __future__ import annotations


def accepts_platform(configured_platform_id: str, event_platform_id: str) -> bool:
    configured = configured_platform_id.strip()
    return bool(configured) and event_platform_id.strip() == configured


def platform_id_from_umo(umo: str) -> str:
    return umo.partition(":")[0].strip()
