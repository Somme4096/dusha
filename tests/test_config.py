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


def test_startup_migrates_configuration_once(tmp_path):
    from dataclasses import asdict

    import yaml

    legacy = {
        "data_dir": str(tmp_path / "data"),
        "memory": {
            "child_chars": 900,
            "child_overlap_chars": 80,
            "embedding": {"base_url": "http://localhost/v1", "model": "local"},
        },
        "proactive": {"minimum_silence_minutes": 90, "failed_retry_minutes": 12},
    }
    canonical = {
        "data_dir": str(tmp_path / "data"),
        "embedding": legacy["memory"]["embedding"],
        "memory": {"chunk_max_chars": 900, "chunk_overlap_chars": 80},
        "proactive": {"min_silence_minutes": 90, "retry_delay_minutes": 12},
    }
    old_path, new_path = tmp_path / "old.yaml", tmp_path / "new.yaml"
    old_path.write_text(yaml.safe_dump(legacy))
    new_path.write_text(yaml.safe_dump(canonical))
    old_path.chmod(0o600)
    assert asdict(load_config(old_path)) == asdict(load_config(new_path))
    assert yaml.safe_load(old_path.read_text()) == canonical
    assert old_path.stat().st_mode & 0o777 == 0o600
    first_write = old_path.stat().st_mtime_ns
    load_config(old_path)
    assert old_path.stat().st_mtime_ns == first_write


def test_migration_keeps_canonical_keys_and_removes_semantic_controls(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(f"""
data_dir: {tmp_path / "data"}
embedding:
  base_url: http://localhost/v1
  model: canonical
memory:
  embedding:
    model: legacy
  child_chars: 900
  chunk_max_chars: 1000
  semantic_min_similarity: 0.99
affect:
  semantic_enabled: false
  semantic_min_similarity: 0.99
proactive:
  minimum_silence_minutes: 90
  min_silence_minutes: 100
""")
    config = load_config(path)
    assert config.embedding.model == "canonical"
    assert config.memory.chunk_max_chars == 1000
    assert config.proactive.min_silence_minutes == 100
    from companion_gateway.service import CompanionService

    service = CompanionService(config)
    assert service.affect.appraisal is not None
    assert not hasattr(config.affect, "semantic_enabled")
    assert not hasattr(config.memory, "semantic_min_similarity")
    import yaml

    saved = yaml.safe_load(path.read_text())
    assert saved["affect"] == {}
    assert saved["memory"] == {"chunk_max_chars": 1000}
    assert saved["proactive"] == {"min_silence_minutes": 100}


def test_failed_migration_write_preserves_original(tmp_path, monkeypatch):
    import pytest

    path = tmp_path / "config.yaml"
    original = f"data_dir: {tmp_path / 'data'}\nmemory:\n  child_chars: 900\n"
    path.write_text(original)

    def fail_replace(*args):
        raise OSError("write failed")

    monkeypatch.setattr("companion_gateway.config.os.replace", fail_replace)
    with pytest.raises(OSError, match="write failed"):
        load_config(path)
    assert path.read_text() == original
    assert list(tmp_path.iterdir()) == [path]
