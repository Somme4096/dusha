from __future__ import annotations

from collections.abc import Collection


def accepts_route(
    platform_id: str, config_profile: str, event_platform_id: str, event_profiles: Collection[str]
) -> bool:
    platform = platform_id.strip()
    profile = config_profile.strip()
    if not platform and not profile:
        return False
    if platform and event_platform_id.strip() != platform:
        return False
    return not profile or profile in event_profiles


def platform_id_from_umo(umo: str) -> str:
    return umo.partition(":")[0].strip()
