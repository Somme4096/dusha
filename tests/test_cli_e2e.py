from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def cli_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path / "data")}), encoding="utf-8")
    return config


def _run(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    return subprocess.run(
        [sys.executable, "-m", "companion_gateway.cli", "--config", str(config), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_cli_success_and_persistence_across_process_restarts(cli_config: Path) -> None:
    config = cli_config
    remembered = _run(config, "evergreen", "remember", "favorite", "green tea")
    listed = _run(config, "evergreen", "list")

    assert remembered.returncode == 0
    assert json.loads(remembered.stdout)["fact"]["key"] == "favorite"
    assert listed.returncode == 0
    assert any(
        fact["key"] == "favorite" and fact["text"] == "green tea"
        for fact in json.loads(listed.stdout)["facts"]
    )


def test_cli_failure_path_uses_nonzero_exit_and_stderr(cli_config: Path) -> None:
    config = cli_config
    result = _run(config, "memory", "show", "999999")

    assert result.returncode == 1
    assert result.stderr.rstrip().endswith("message not found")


@pytest.mark.parametrize(
    ("args", "expected_keys"),
    [
        (("health",), ("database",)),
        (("memory", "recent"), ("messages",)),
        (("memory", "reindex"), ("status",)),
        (("affect", "show"), ("base", "mood")),
    ],
)
def test_cli_commands_run_in_a_subprocess(
    cli_config: Path, args: tuple[str, ...], expected_keys: tuple[str, ...]
) -> None:
    config = cli_config
    result = _run(config, *args)

    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert all(key in output for key in expected_keys)
    if args == ("health",):
        assert output["database"] == "ok"
    elif args == ("memory", "recent"):
        assert output["messages"] == []
    elif args == ("memory", "reindex"):
        assert output["status"] == "rebuilt"


def test_cli_rejects_yaml_config_in_a_subprocess(tmp_path: Path) -> None:
    source = tmp_path / "legacy.yaml"
    source.write_text("host: example-host\n", encoding="utf-8")

    result = _run(source, "health")

    assert result.returncode != 0
    assert "unsupported configuration format" in result.stderr


def test_cli_migration_command_is_unavailable(cli_config: Path) -> None:
    result = _run(cli_config, "migrate-config", "old.yaml", "new.json")

    assert result.returncode == 2
    assert "invalid choice" in result.stderr
