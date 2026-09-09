from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).parents[1] / "integrations" / "astrbot_companion_gateway" / "config_migration.py"
_SPEC = importlib.util.spec_from_file_location("astrbot_config_migration", _PATH)
assert _SPEC and _SPEC.loader
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)


def test_plugin_config_migrates_before_schema_normalization_once(tmp_path):
    path = tmp_path / "plugin_config.json"
    path.write_text(json.dumps({"poll_seconds": 45, "enable_proactive": False, "platform_id": "bot"}))
    migration.migrate_config(path)
    assert json.loads(path.read_text()) == {
        "poll_interval_seconds": 45,
        "proactive_enabled": False,
        "platform_id": "bot",
    }
    timestamp = path.stat().st_mtime_ns
    migration.migrate_config(path)
    assert path.stat().st_mtime_ns == timestamp


def test_plugin_migration_preserves_canonical_values(tmp_path):
    path = tmp_path / "plugin_config.json"
    path.write_text(json.dumps({"poll_seconds": 45, "poll_interval_seconds": 60}))
    migration.migrate_config(path)
    assert json.loads(path.read_text()) == {"poll_interval_seconds": 60}
    migration.migrate_config(tmp_path / "missing.json")
    assert not (tmp_path / "missing.json").exists()
