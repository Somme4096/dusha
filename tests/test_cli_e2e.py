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
        [sys.executable, "-m", "dusha.cli", "--config", str(config), *args],
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


def test_cli_memo_lifecycle_across_process_restarts(cli_config: Path) -> None:
    config = cli_config
    added = _run(config, "memo", "add", "Draft the yumecho report.")
    assert added.returncode == 0
    note = json.loads(added.stdout)["memo"]

    listed = _run(config, "memo", "list")
    assert listed.returncode == 0
    assert any(memo["id"] == note["id"] for memo in json.loads(listed.stdout)["memos"])

    empty_reason = _run(config, "memo", "done", str(note["id"]), "   ")
    assert empty_reason.returncode != 0

    done = _run(config, "memo", "done", str(note["id"]), "Published.")
    assert done.returncode == 0
    assert json.loads(done.stdout)["memo"]["status"] == "archived"

    active = _run(config, "memo", "list")
    assert json.loads(active.stdout)["memos"] == []
    archived = _run(config, "memo", "list", "--status", "archived")
    assert json.loads(archived.stdout)["memos"][0]["reason"] == "Published."


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
    source = tmp_path / "config.yaml"
    source.write_text("host: example-host\n", encoding="utf-8")

    result = _run(source, "health")

    assert result.returncode != 0
    assert "unsupported configuration format" in result.stderr


def _run_in_root(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "XDG_CONFIG_HOME": str(root),
    }
    return subprocess.run(
        [sys.executable, "-m", "dusha.cli", *args], capture_output=True, text=True, env=env, check=False
    )


def test_cli_companions_keep_separate_state_across_restarts(tmp_path: Path) -> None:
    for name in ("dusha", "luna"):
        home = tmp_path / "dusha" / name
        home.mkdir(parents=True)
        (home / "config.json").write_text("{}", encoding="utf-8")

    listed = _run_in_root(tmp_path, "list")
    assert json.loads(listed.stdout) == {
        "root": str(tmp_path / "dusha"), "companions": ["dusha", "luna"],
    }

    added = _run_in_root(tmp_path, "-c", "dusha", "memo", "add", "Only Dusha knows this.")
    assert added.returncode == 0
    dusha = json.loads(_run_in_root(tmp_path, "--companion", "dusha", "memo", "list").stdout)
    luna = json.loads(_run_in_root(tmp_path, "--home", str(tmp_path / "dusha" / "luna"), "memo", "list").stdout)
    assert [memo["text"] for memo in dusha["memos"]] == ["Only Dusha knows this."]
    assert luna["memos"] == []
    assert (tmp_path / "dusha" / "dusha" / "data" / "state.sqlite3").is_file()
    assert (tmp_path / "dusha" / "luna" / "data" / "state.sqlite3").is_file()


def test_cli_rejects_ambiguous_and_unknown_companions(tmp_path: Path) -> None:
    for name in ("dusha", "luna"):
        home = tmp_path / "dusha" / name
        home.mkdir(parents=True)
        (home / "config.json").write_text("{}", encoding="utf-8")

    ambiguous = _run_in_root(tmp_path, "memo", "list")
    unknown = _run_in_root(tmp_path, "-c", "nobody", "memo", "list")
    assert ambiguous.returncode == 2
    assert "several companions exist" in ambiguous.stderr
    assert unknown.returncode == 2
    assert "configuration file not found" in unknown.stderr
