from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from dusha import api
from dusha.config import load_config

SRC = str(Path(__file__).parents[1] / "src")


def _env(root: Path, **extra: str) -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": SRC, "XDG_CONFIG_HOME": str(root), **extra}


def _cli(root: Path, *args: str, stdin: str = "", **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dusha.cli", *args], input=stdin, capture_output=True, text=True,
        env=_env(root, **env), check=False, start_new_session=True,
    )


def _home(root: Path, name: str, **raw) -> Path:
    path = root / "dusha" / name / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


@pytest.fixture
def serving(run_app):
    @contextmanager
    def start(path: Path):
        cfg = load_config(path)
        with run_app(api.create_app(cfg), port=cfg.port) as url:
            yield url

    return start


def _json(result: subprocess.CompletedProcess[str]):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cli_runs_plugin_commands_on_the_server_and_state_survives_a_restart(tmp_path, free_port, serving):
    path = _home(tmp_path, "nova", port=free_port())
    with serving(path):
        remembered = _json(
            _cli(tmp_path, "evergreen", "remember", "favorite", "green tea", "--priority", "70")
        )
        facts = _json(_cli(tmp_path, "evergreen", "list"))
        fact = remembered["fact"]
        clash = _cli(
            tmp_path, "evergreen", "revise", fact["fact_id"], "1", "black tea",
            "--review-after", "2027-01-01T00:00:00+00:00", "--clear-review-after",
        )
        revised = _json(_cli(tmp_path, "evergreen", "revise", fact["fact_id"], "1", "black tea"))
        note = _json(_cli(tmp_path, "memo", "add", "Draft the yumecho report."))["memo"]
        blank = _cli(tmp_path, "memo", "done", str(note["id"]), "   ")
        done = _json(_cli(tmp_path, "memo", "done", str(note["id"]), "Published."))
        missing = _cli(tmp_path, "memory", "show", "999999")
        bad_type = _cli(tmp_path, "memo", "done", "not-a-number", "Published.")
        bad_choice = _cli(tmp_path, "memo", "list", "--status", "lost")
        health = _json(_cli(tmp_path, "health"))
        recent = _json(_cli(tmp_path, "memory", "recent"))
        reindex = _json(_cli(tmp_path, "memory", "reindex"))
        affect = _json(_cli(tmp_path, "affect", "show"))
        backup = _json(_cli(tmp_path, "backup", str(tmp_path / "backup.sqlite3")))
    with serving(path):
        archived = _json(_cli(tmp_path, "memo", "list", "--status", "archived"))
        active = _json(_cli(tmp_path, "memo", "list"))
    assert fact["key"] == "favorite" and fact["priority"] == 70
    assert [(item["key"], item["text"]) for item in facts["facts"]] == [("favorite", "green tea")]
    assert clash.returncode == 1
    assert "review_after and clear_review_after cannot be used together" in clash.stderr
    assert revised["fact"]["text"] == "black tea" and revised["fact"]["revision"] == 2
    assert blank.returncode == 1 and blank.stderr.strip()
    assert done["memo"]["status"] == "archived"
    assert missing.returncode == 1 and missing.stderr.rstrip().endswith("message not found")
    assert bad_type.returncode == 2 and "not-a-number" in bad_type.stderr
    assert bad_choice.returncode == 2 and "lost" in bad_choice.stderr
    assert health == {"database": "ok"}
    assert recent == {"messages": []}
    assert reindex == {"status": "rebuilt"}
    assert {"base", "mood"} <= set(affect)
    assert Path(backup["backup"]).is_file()
    assert [memo["reason"] for memo in archived["memos"]] == ["Published."]
    assert active == {"memos": []}


def test_cli_sends_the_token_and_prefers_the_saved_server(tmp_path, free_port, serving):
    server = _home(tmp_path, "nova", port=free_port(), api_token="server-secret")
    url = f"http://127.0.0.1:{json.loads(server.read_text())['port']}"
    _home(tmp_path, "client", proxy={"server": {"url": url, "token": "server-secret"}})
    _home(tmp_path, "stale", proxy={"server": {"url": url, "token": "wrong"}})
    with serving(server):
        local = _json(_cli(tmp_path, "-c", "nova", "memo", "add", "From the server machine."))
        remote = _json(_cli(tmp_path, "-c", "client", "memo", "list"))
        plugins = _json(_cli(tmp_path, "-c", "client", "plugins"))
        denied = _cli(tmp_path, "-c", "stale", "memo", "list")
        denied_plugins = _cli(tmp_path, "-c", "stale", "plugins")
    assert [memo["id"] for memo in remote["memos"]] == [local["memo"]["id"]]
    assert plugins["memo"]["state"] == "active"
    assert denied.returncode == 1 and "invalid companion token" in denied.stderr
    assert denied_plugins.returncode == 1 and "invalid companion token" in denied_plugins.stderr


def test_cli_hides_the_commands_of_disabled_plugins(tmp_path, free_port, serving):
    path = _home(
        tmp_path, "nova", port=free_port(),
        plugins={"memory": {"disabled": True}, "evergreen": {"disabled": True}},
    )
    with serving(path):
        listed = _cli(tmp_path, "--help")
        plugins = _json(_cli(tmp_path, "plugins"))
        memos = _json(_cli(tmp_path, "memo", "list"))
        refused = [_cli(tmp_path, *args) for args in (("memory", "recent"), ("evergreen", "list"))]
    static, plugin = listed.stdout.split("Core commands:")[1].split("Plugin commands:")
    commands = re.findall(r"^  (\S+)", static, re.MULTILINE)
    plugin_commands = re.findall(r"^  (\S+)", plugin, re.MULTILINE)
    assert listed.returncode == 0
    assert set(commands) == {"init", "serve", "proxy", "provider", "list", "plugins", "health", "backup"}
    assert "memo" in plugin_commands and "memory" not in plugin_commands and "evergreen" not in plugin_commands
    assert plugins["memory"]["state"] == plugins["evergreen"]["state"] == "disabled"
    assert memos == {"memos": []}
    for result, name in zip(refused, ("memory", "evergreen"), strict=True):
        assert result.returncode == 2
        assert f"No such command '{name}'" in result.stderr


def test_cli_help_works_offline_from_the_cached_command_list(tmp_path, free_port, serving):
    path = _home(tmp_path, "nova", port=free_port())
    with serving(path):
        online = _cli(tmp_path, "memo", "--help")
    offline = _cli(tmp_path, "memo", "--help")
    run = _cli(tmp_path, "memo", "list")
    _home(tmp_path, "nova", port=free_port())
    moved = _cli(tmp_path, "--help")
    assert online.returncode == offline.returncode == 0
    assert offline.stdout == online.stdout and "done" in offline.stdout
    assert "using the cached command list" in offline.stderr
    assert run.returncode == 1 and "cannot reach the server at http://127.0.0.1:" in run.stderr
    assert moved.returncode == 0 and "memo" not in moved.stdout and "serve" in moved.stdout


def test_cli_resolves_companions_in_the_documented_order(tmp_path, free_port):
    ports = {}
    for name in ("dusha", "luna"):
        ports[name] = free_port()
        _home(tmp_path, name, port=ports[name])
    ports["elsewhere"], ports["explicit"] = free_port(), free_port()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "config.json").write_text(json.dumps({"port": ports["elsewhere"]}), encoding="utf-8")
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"port": ports["explicit"]}), encoding="utf-8")
    names = {port: name for name, port in ports.items()}

    def selected(*args: str, **env: str) -> str:
        result = _cli(tmp_path, *args, "memo", "list", **env)
        assert result.returncode == 1, result.stderr
        return names[int(re.search(r"http://127\.0\.0\.1:(\d+)", result.stderr)[1])]

    both = {"DUSHA_COMPANION": "luna", "DUSHA_HOME": str(elsewhere)}
    assert selected(DUSHA_COMPANION="luna") == "luna"
    assert selected(**both) == "elsewhere"
    assert selected("-c", "dusha", **both) == "dusha"
    assert selected("--home", str(tmp_path / "dusha" / "luna"), "-c", "dusha", **both) == "luna"
    assert selected("--config", str(explicit), "--home", str(elsewhere), **both) == "explicit"

    ambiguous = _cli(tmp_path, "memo", "list")
    unknown = _cli(tmp_path, "-c", "nobody", "memo", "list")
    escaped = _cli(tmp_path, "-c", "../escape", "memo", "list")
    absent = _cli(tmp_path, "--config", str(tmp_path / "missing.json"), "memo", "list")
    (tmp_path / "config.yaml").write_text("host: example-host\n", encoding="utf-8")
    yaml = _cli(tmp_path, "--config", str(tmp_path / "config.yaml"), "health")
    assert ambiguous.returncode == 2 and "several companions exist" in ambiguous.stderr
    assert unknown.returncode == 2 and "configuration file not found" in unknown.stderr
    assert escaped.returncode == 2 and "companion name" in escaped.stderr
    assert absent.returncode == 2 and "configuration file not found" in absent.stderr
    assert yaml.returncode == 2 and "unsupported configuration format" in yaml.stderr
    assert _json(_cli(tmp_path, "list")) == {"root": str(tmp_path / "dusha"), "companions": ["dusha", "luna"]}


def test_cli_talks_to_the_only_companion_without_a_selection(tmp_path, free_port, serving):
    path = _home(tmp_path, "solo", port=free_port())
    with serving(path):
        added = _json(_cli(tmp_path, "memo", "add", "Solo memo."))
    assert added["memo"]["text"] == "Solo memo."
    assert (path.parent / "data" / "state.sqlite3").is_file()


def _start(root: Path, *args: str, stdin: str = "", **env: str) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-m", "dusha.cli", *args], env=_env(root, **env), text=True,
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True,
    )
    process.stdin.write(stdin)
    process.stdin.close()
    return process


def _stop(process: subprocess.Popen[str]) -> str:
    process.terminate()
    process.wait(timeout=10)
    with process.stderr:
        return process.stderr.read()


def _wait_for(check, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


def _get_json(url: str):
    try:
        return httpx.get(url, timeout=1).json()
    except httpx.HTTPError:
        return None


def test_cli_serve_creates_the_config_once_and_serves_it_after_restart(tmp_path, free_port):
    config = tmp_path / "dusha" / "nova" / "config.json"
    example = json.loads((Path(__file__).parents[1] / "config.example.json").read_text(encoding="utf-8"))

    first = _start(tmp_path, "serve", "nova")
    try:
        _wait_for(config.is_file)
    finally:
        stderr = _stop(first)
    assert f"created {config}" in stderr
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    generated = json.loads(config.read_text(encoding="utf-8"))
    assert generated == example

    port = free_port()
    config.write_text(json.dumps({**generated, "port": port}), encoding="utf-8")
    second = _start(tmp_path, "serve", "nova")
    try:
        health = _wait_for(lambda: _get_json(f"http://127.0.0.1:{port}/health"))
    finally:
        stderr = _stop(second)
    assert health["status"] == "ok" and health["companion"] == "nova"
    assert "created" not in stderr


def test_cli_serve_follows_the_selection_order(tmp_path, free_port):
    ports = {name: free_port() for name in ("dusha", "luna")}
    for name, port in ports.items():
        _home(tmp_path, name, port=port)
    server = _start(tmp_path, "serve", DUSHA_COMPANION="luna")
    try:
        health = _wait_for(lambda: _get_json(f"http://127.0.0.1:{ports['luna']}/health"))
    finally:
        _stop(server)
    ambiguous = _cli(tmp_path, "serve")
    assert health["companion"] == "luna"
    assert ambiguous.returncode == 2 and "several companions exist" in ambiguous.stderr


def test_cli_serve_rejects_a_bad_companion_name_without_creating_anything(tmp_path):
    result = _cli(tmp_path, "serve", "../escape")
    assert result.returncode == 2
    assert "companion name" in result.stderr
    assert not (tmp_path / "dusha").exists()


def test_cli_proxy_asks_for_the_server_once_and_saves_it(tmp_path, free_port, serving):
    server = _home(tmp_path, "nova", port=free_port(), api_token="server-secret")
    url = f"http://127.0.0.1:{json.loads(server.read_text())['port']}"
    config = tmp_path / "dusha" / "edge" / "config.json"

    first = _start(tmp_path, "proxy", "edge", stdin=f"ftp://nowhere\n{url}/\nserver-secret\n")
    try:
        _wait_for(config.is_file)
    finally:
        stderr = _stop(first)
    assert "Server URL" in stderr and "must be an http or https URL" in stderr
    assert f"created {config}" in stderr
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["proxy"]["server"] == {"url": url, "token": "server-secret", "timeout_seconds": 10}
    assert "providers" not in saved

    port = free_port()
    saved["proxy"]["port"] = port
    config.write_text(json.dumps(saved), encoding="utf-8")
    with serving(server):
        again = _start(tmp_path, "proxy", "edge")
        try:
            health = _wait_for(lambda: _get_json(f"http://127.0.0.1:{port}/health"))
        finally:
            stderr = _stop(again)
        memo = _json(_cli(tmp_path, "-c", "edge", "memo", "add", "Through the saved server."))
    assert health["server"] == {"url": url, "status": "ok"}
    assert "Server URL" not in stderr and "created" not in stderr
    assert memo["memo"]["text"] == "Through the saved server."


def test_cli_proxy_reaches_the_server_in_the_same_config(tmp_path, free_port, serving):
    server_port, proxy_port = free_port(), free_port()
    path = _home(tmp_path, "nova", port=server_port, api_token="t", proxy={"port": proxy_port})
    with serving(path):
        proxy = _start(tmp_path, "proxy", "nova")
        try:
            health = _wait_for(lambda: _get_json(f"http://127.0.0.1:{proxy_port}/health"))
        finally:
            stderr = _stop(proxy)
    assert health["server"] == {"url": f"http://127.0.0.1:{server_port}", "status": "ok"}
    assert "Server URL" not in stderr


def test_cli_proxy_needs_a_companion_name(tmp_path):
    result = _cli(tmp_path, "proxy")
    assert result.returncode == 2 and "name a companion" in result.stderr
    assert not (tmp_path / "dusha").exists()


def test_cli_init_sets_up_both_parts_once(tmp_path):
    config = tmp_path / "dusha" / "nova" / "config.json"
    first = _cli(tmp_path, "init", "nova")
    created = config.read_text(encoding="utf-8")
    again = _cli(tmp_path, "init", "nova")
    saved = json.loads(created)
    assert first.returncode == 0 and f"created {config}" in first.stderr
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert saved["port"] == 8765 and "plugins" in saved
    assert saved["proxy"]["server"]["url"] == "http://127.0.0.1:8765"
    assert again.returncode == 0 and "nova is up to date" in again.stderr
    assert config.read_text(encoding="utf-8") == created


def test_cli_init_adds_each_part_without_touching_the_other(tmp_path):
    config = tmp_path / "dusha" / "edge" / "config.json"
    proxy = _cli(tmp_path, "init", "edge", "--proxy", "--server-url", "http://10.0.0.2:8765", "--token", "t")
    client_only = json.loads(config.read_text(encoding="utf-8"))
    serve = _cli(tmp_path, "init", "edge", "--serve")
    both = json.loads(config.read_text(encoding="utf-8"))
    assert proxy.returncode == 0 and set(client_only) == {"proxy"}
    server = client_only["proxy"]["server"]
    assert server == {"url": "http://10.0.0.2:8765", "token": "t", "timeout_seconds": 10}
    assert serve.returncode == 0 and f"added the server keys to {config}" in serve.stderr
    assert both["proxy"] == client_only["proxy"] and both["port"] == 8765


def test_cli_serve_adds_the_server_keys_to_a_proxy_only_home(tmp_path, free_port):
    port = free_port()
    config = _home(tmp_path, "edge", proxy={"server": {"url": "http://10.0.0.2:8765", "token": ""}})
    original = json.loads(config.read_text(encoding="utf-8"))
    server = _start(tmp_path, "serve", "edge")
    try:
        _wait_for(lambda: "plugins" in json.loads(config.read_text(encoding="utf-8")))
    finally:
        stderr = _stop(server)
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert f"added the server keys to {config}" in stderr
    assert saved["proxy"] == original["proxy"] and saved["port"] == 8765
    config.write_text(json.dumps({**saved, "port": port}), encoding="utf-8")
    again = _start(tmp_path, "serve", "edge")
    try:
        health = _wait_for(lambda: _get_json(f"http://127.0.0.1:{port}/health"))
    finally:
        stderr = _stop(again)
    assert health["status"] == "ok" and "added" not in stderr


def test_cli_init_upgrades_a_schema_3_prompts_file_and_keeps_a_backup(tmp_path):
    state = {"open_delimiter": "<s>", "close_delimiter": "</s>"}
    legacy = {"companion_state": {**state, "memory_instruction": "M"}}
    config = _home(tmp_path, "nova", prompts={"path": "prompts.json"})
    prompts = config.parent / "prompts.json"
    prompts.write_text(json.dumps(legacy), encoding="utf-8")
    first = _cli(tmp_path, "init", "nova", "--serve")
    again = _cli(tmp_path, "init", "nova", "--serve")
    assert first.returncode == 0 and f"upgraded {prompts}" in first.stderr
    assert json.loads(prompts.read_text(encoding="utf-8")) == {
        "companion_state": state,
        "plugins": {"memory": {"instruction": "M"}},
    }
    assert json.loads((config.parent / "prompts.json.bak").read_text(encoding="utf-8")) == legacy
    assert "upgraded" not in again.stderr and "nova is up to date" in again.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("init",), "name a companion"),
        (("--config", "x.json", "init"), "not --config"),
        (("init", "nova", "--proxy", "--server-url", "ftp://nowhere"), "http or https URL"),
    ],
)
def test_cli_init_rejects_a_bad_selection_without_creating_anything(tmp_path, args, message):
    result = _cli(tmp_path, *args)
    assert result.returncode == 2 and message in result.stderr
    assert not (tmp_path / "dusha").exists()


def test_cli_init_moves_proxy_upstreams_to_providers_and_keeps_a_backup(tmp_path):
    upstream = {"base_url": "https://a.invalid/v1", "key": "sk-a"}
    proxy = {"server": {"url": "http://127.0.0.1:8765"}, "upstreams": {"a": upstream}}
    config = _home(tmp_path, "nova", port=8765, proxy=proxy)
    config.chmod(0o600)
    original = config.read_text(encoding="utf-8")
    result = _cli(tmp_path, "init", "nova")
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert result.returncode == 0 and f"moved proxy.upstreams to providers in {config}" in result.stderr
    assert saved["providers"] == {"a": upstream} and "upstreams" not in saved["proxy"]
    assert (config.parent / "config.json.bak").read_text(encoding="utf-8") == original
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
