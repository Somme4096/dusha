from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    return subprocess.run(
        [sys.executable, "-m", "companion_gateway.cli", "--config", str(config), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_cli_success_and_persistence_across_process_restarts(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path / "data")}), encoding="utf-8")

    remembered = _run(config, "evergreen", "remember", "favorite", "green tea")
    listed = _run(config, "evergreen", "list")

    assert remembered.returncode == 0
    assert json.loads(remembered.stdout)["fact"]["key"] == "favorite"
    assert listed.returncode == 0
    assert any(fact["text"] == "green tea" for fact in json.loads(listed.stdout)["facts"])


def test_cli_failure_path_uses_nonzero_exit_and_stderr(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path / "data")}), encoding="utf-8")

    result = _run(config, "memory", "show", "999999")

    assert result.returncode != 0
    assert result.stderr.rstrip().endswith("message not found")
