from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
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


def _run_in_root(root: Path, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "XDG_CONFIG_HOME": str(root),
        **extra_env,
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
    luna_home = str(tmp_path / "dusha" / "luna")
    luna = json.loads(_run_in_root(tmp_path, "--home", luna_home, "memo", "list").stdout)
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


def test_cli_companion_selection_follows_the_documented_order(tmp_path: Path) -> None:
    homes = {name: tmp_path / "dusha" / name for name in ("dusha", "luna")}
    homes["elsewhere"] = tmp_path / "elsewhere"
    for name, home in homes.items():
        home.mkdir(parents=True)
        (home / "config.json").write_text("{}", encoding="utf-8")
        assert _run_in_root(tmp_path, "--home", str(home), "memo", "add", name).returncode == 0
    explicit = tmp_path / "explicit" / "config.json"
    explicit.parent.mkdir()
    explicit.write_text("{}", encoding="utf-8")
    assert _run_in_root(tmp_path, "--config", str(explicit), "memo", "add", "explicit").returncode == 0

    def selected(*args: str, **env: str) -> list[str]:
        result = _run_in_root(tmp_path, *args, "memo", "list", **env)
        assert result.returncode == 0, result.stderr
        return [memo["text"] for memo in json.loads(result.stdout)["memos"]]

    both = {"DUSHA_COMPANION": "luna", "DUSHA_HOME": str(homes["elsewhere"])}
    assert selected(DUSHA_COMPANION="luna") == ["luna"]
    assert selected(**both) == ["elsewhere"]
    assert selected("-c", "dusha", **both) == ["dusha"]
    assert selected("--home", str(homes["luna"]), "-c", "dusha", **both) == ["luna"]
    assert selected("--config", str(explicit), "--home", str(homes["luna"]), **both) == ["explicit"]

    escaped = _run_in_root(tmp_path, "-c", "../escape", "memo", "list")
    missing = _run_in_root(tmp_path, "--config", str(tmp_path / "missing.json"), "memo", "list")
    assert escaped.returncode == 2
    assert "companion name" in escaped.stderr
    assert missing.returncode == 2
    assert "configuration file not found" in missing.stderr


def test_cli_serves_the_only_companion_without_a_selection(tmp_path: Path) -> None:
    home = tmp_path / "dusha" / "solo"
    home.mkdir(parents=True)
    (home / "config.json").write_text("{}", encoding="utf-8")

    added = _run_in_root(tmp_path, "memo", "add", "Solo memo.")

    assert added.returncode == 0, added.stderr
    assert (home / "data" / "state.sqlite3").is_file()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(root: Path, *args: str) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src"), "XDG_CONFIG_HOME": str(root)}
    return subprocess.Popen(
        [sys.executable, "-m", "dusha.cli", "serve", *args], env=env, text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def _wait_for(check, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


def test_cli_serve_creates_the_config_once_and_serves_it_after_restart(tmp_path: Path) -> None:
    config = tmp_path / "dusha" / "nova" / "config.json"
    example = json.loads((Path(__file__).parents[1] / "config.example.json").read_text(encoding="utf-8"))

    first = _serve(tmp_path, "nova")
    try:
        _wait_for(config.is_file)
    finally:
        first.terminate()
        _, stderr = first.communicate(timeout=10)
    assert f"created {config}" in stderr
    generated = json.loads(config.read_text(encoding="utf-8"))
    assert generated == example

    port = _free_port()
    config.write_text(json.dumps({**generated, "port": port}), encoding="utf-8")
    second = _serve(tmp_path, "nova")
    try:
        def healthy():
            try:
                return httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).json()
            except httpx.HTTPError:
                return None

        health = _wait_for(healthy)
    finally:
        second.terminate()
        _, stderr = second.communicate(timeout=10)
    assert health["status"] == "ok"
    assert health["companion"] == "nova"
    assert "created" not in stderr
    assert json.loads(config.read_text(encoding="utf-8"))["port"] == port


def test_cli_serve_rejects_a_bad_companion_name_without_creating_anything(tmp_path: Path) -> None:
    result = _serve(tmp_path, "../escape")
    _, stderr = result.communicate(timeout=10)
    assert result.returncode == 2
    assert "companion name" in stderr
    assert not (tmp_path / "dusha").exists()
