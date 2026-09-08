from companion_gateway.config import load_config


def _write_config(path, *, port):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"port: {port}\ndata_dir: {path.parent / 'data'}\n", encoding="utf-8")


def test_config_precedence_is_explicit_then_environment_then_user_then_cwd(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.yaml"
    env_path = tmp_path / "env.yaml"
    user_path = tmp_path / "xdg" / "companion-gateway" / "config.yaml"
    cwd_path = tmp_path / "config.yaml"
    for config_path, port in ((explicit, 1), (env_path, 2), (user_path, 3), (cwd_path, 4)):
        _write_config(config_path, port=port)

    monkeypatch.setenv("COMPANION_GATEWAY_CONFIG", str(env_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    assert load_config(explicit).port == 1
    assert load_config().port == 2

    monkeypatch.delenv("COMPANION_GATEWAY_CONFIG")
    assert load_config().port == 3

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing-xdg"))
    assert load_config().port == 4


def test_user_config_is_selected_from_neutral_cwd_with_expanded_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    user_path = home / ".config" / "companion-gateway" / "config.yaml"
    _write_config(user_path, port=4321)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("COMPANION_GATEWAY_CONFIG", raising=False)
    neutral_cwd = tmp_path / "neutral"
    neutral_cwd.mkdir()
    monkeypatch.chdir(neutral_cwd)

    assert load_config().port == 4321


def test_missing_explicit_path_keeps_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert load_config("~/missing-companion-gateway.yaml").port == 8765
