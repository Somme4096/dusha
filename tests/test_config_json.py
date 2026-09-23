from __future__ import annotations

import json
from pathlib import Path

import pytest

from companion_gateway import emotions
from companion_gateway.config import AffectConfig, AppConfig, load_config, migrate_config
from companion_gateway.emotions import (
    default_fingerprint,
    fingerprint,
    load_emotions,
    resolve_emotions,
)
from companion_gateway.service import CompanionService


def test_config_example_json_loads(tmp_path):
    example = Path(__file__).parents[1] / "config.example.json"
    data = json.loads(example.read_text(encoding="utf-8"))
    data["data_dir"] = str(tmp_path / "data")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg = load_config(config)
    assert cfg.port == 8765
    assert cfg.timezone == "Asia/Taipei"
    assert cfg.upstream.base_url == "https://api.openai.com/v1"
    assert cfg.memory.retrieval_mode == "hybrid"
    assert cfg.affect.classification_fallback_seconds == 120
    assert cfg.proactive.longing_threshold == 0.48
    assert cfg.proactive.fear_threshold == 0.55


def test_config_example_yaml_still_loads(tmp_path):
    example = Path(__file__).parents[1] / "config.example.yaml"
    text = example.read_text(encoding="utf-8")
    text = text.replace("~/.local/share/companion-gateway", str(tmp_path / "data"))
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")
    with pytest.warns(DeprecationWarning):
        cfg = load_config(config)
    assert cfg.port == 8765
    assert cfg.upstream.base_url == "https://api.openai.com/v1"
    assert cfg.memory.retrieval_mode == "hybrid"
    assert cfg.affect.dimensions["fear"]["neutral"] == 0.0
    assert cfg.proactive.quiet_end_hour == 8


def test_json_config_rejects_duplicate_keys(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"host": "a", "host": "b"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key"):
        load_config(config)


def test_json_config_rejects_unknown_fields(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"host": "a", "nonsense": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown top-level field"):
        load_config(config)
    config.write_text('{"memory": {"bogus": 1}}', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown field\\(s\\) under memory"):
        load_config(config)
    config.write_text('{"affect": {"bogus": 1}}', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown field\\(s\\) under affect"):
        load_config(config)
    config.write_text('{"emotions": {"bogus": 1}}', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown field\\(s\\) under emotions"):
        load_config(config)


def test_json_config_rejects_non_finite_and_comments(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"upstream": {"timeout_seconds": NaN}}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite"):
        load_config(config)
    config.write_text('{"host": "a" // jsonc comment\n}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_config(config)


def test_json_config_rejects_wrong_types(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"port": "abc"}', encoding="utf-8")
    with pytest.raises(ValueError, match="port must be int"):
        load_config(config)
    config.write_text('{"proactive": {"enabled": "yes"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="enabled must be bool"):
        load_config(config)
    config.write_text('{"memory": {"recent_messages": true}}', encoding="utf-8")
    with pytest.raises(ValueError, match="recent_messages must be int"):
        load_config(config)


def test_yaml_config_rejects_duplicates_and_unknown_fields(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("host: a\nhost: b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key"):
        load_config(config)
    config.write_text("bogus: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown top-level field"):
        load_config(config)


def test_config_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPANION_GATEWAY_CONFIG", raising=False)
    (tmp_path / "config.json").write_text('{"host": "from-json"}', encoding="utf-8")
    (tmp_path / "config.yaml").write_text("host: from-yaml\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_config().host == "from-json"

    (tmp_path / "config.json").unlink()
    with pytest.warns(DeprecationWarning):
        assert load_config().host == "from-yaml"

    (tmp_path / "config.yaml").unlink()
    assert load_config().host == "127.0.0.1"


def test_explicit_and_env_config_errors_when_missing(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError, match="configuration file not found"):
        load_config(tmp_path / "missing.json")
    monkeypatch.setenv("COMPANION_GATEWAY_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(FileNotFoundError, match="COMPANION_GATEWAY_CONFIG"):
        load_config()


def test_explicit_config_wins_over_env(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.json"
    explicit.write_text('{"host": "explicit"}', encoding="utf-8")
    env = tmp_path / "env.json"
    env.write_text('{"host": "env"}', encoding="utf-8")
    monkeypatch.setenv("COMPANION_GATEWAY_CONFIG", str(env))
    assert load_config(explicit).host == "explicit"


def test_json_paths_resolve_relative_to_config_file(tmp_path):
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    config = conf_dir / "config.json"
    config.write_text(
        json.dumps({"data_dir": "data", "emotions": {"path": "emotions.json"}}),
        encoding="utf-8",
    )
    cfg = load_config(config)
    assert cfg.data_dir == conf_dir / "data"
    assert cfg.affect.emotions_path == str(conf_dir / "emotions.json")

    config.write_text(
        json.dumps(
            {
                "data_dir": str(tmp_path / "abs-data"),
                "emotions": {"path": str(tmp_path / "abs-emotions.json")},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(config)
    assert cfg.data_dir == tmp_path / "abs-data"
    assert cfg.affect.emotions_path == str(tmp_path / "abs-emotions.json")


def test_yaml_data_dir_keeps_cwd_semantics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    config = cfg_dir / "config.yaml"
    config.write_text("data_dir: data\n", encoding="utf-8")
    with pytest.warns(DeprecationWarning):
        cfg = load_config(config)
    assert cfg.data_dir == Path("data")


def test_migrate_config_converts_yaml_preserving_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "legacy.yaml"
    source.write_text(
        "data_dir: data\n"
        "memory:\n"
        "  retrieval_mode: hybrid\n"
        "  embedding:\n"
        "    base_url: https://embedding.invalid/v1\n"
        "    api_key_env: EMBEDDING_API_KEY\n"
        "affect:\n"
        "  dimensions:\n"
        "    fear:\n"
        "      neutral: 0.1\n",
        encoding="utf-8",
    )
    destination = tmp_path / "out" / "config.json"
    result = migrate_config(source, destination)
    assert result["destination"] == str(destination)

    emitted = json.loads(destination.read_text(encoding="utf-8"))
    assert emitted["memory"]["retrieval_mode"] == "hybrid"
    assert emitted["affect"]["dimensions"]["fear"]["neutral"] == 0.1
    assert Path(emitted["data_dir"]).is_absolute()
    assert Path(emitted["data_dir"]) == (Path.cwd() / "data").resolve()

    cfg = load_config(destination)
    assert cfg.memory.retrieval_mode == "hybrid"
    assert cfg.affect.dimensions["fear"]["neutral"] == 0.1


def test_migrate_config_preserves_emotions_path_meaning(tmp_path):
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    source = conf_dir / "config.yaml"
    source.write_text('emotions:\n  path: custom.json\n  expected_version: "0.2.0"\n')
    destination = tmp_path / "elsewhere.json"
    migrate_config(source, destination)
    emitted = json.loads(destination.read_text(encoding="utf-8"))
    assert emitted["emotions"]["expected_version"] == "0.2.0"
    assert Path(emitted["emotions"]["path"]).is_absolute()
    assert Path(emitted["emotions"]["path"]) == (conf_dir / "custom.json").resolve()


def test_migrate_config_refuses_overwrite_without_force(tmp_path):
    source = tmp_path / "a.yaml"
    source.write_text("host: a\n", encoding="utf-8")
    destination = tmp_path / "out.json"
    destination.write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        migrate_config(source, destination)
    migrate_config(source, destination, force=True)


def test_migrate_config_refuses_literal_secret(tmp_path):
    source = tmp_path / "a.yaml"
    source.write_text("upstream:\n  api_key_env: sk-literal-value-not-an-env-name\n")
    with pytest.raises(ValueError, match="environment variable name"):
        migrate_config(source, tmp_path / "out.json")


def test_cli_parser_has_migrate_config_command():
    from companion_gateway.cli import parser

    args = parser().parse_args(["migrate-config", "a.yaml", "b.json"])
    assert args.command == "migrate-config"
    assert args.source == "a.yaml"
    assert args.destination == "b.json"


def test_custom_emotions_file_overrides_defaults(tmp_path):
    base = emotions.default_emotions()
    base["emotion_version"] = "custom-1"
    base["dimensions"]["fear"]["neutral"] = 0.1
    path = tmp_path / "emotions.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")

    loaded = load_emotions(path)
    assert loaded["dimensions"]["fear"]["neutral"] == 0.1

    resolved = resolve_emotions(AffectConfig(emotions_path=str(path)))
    assert resolved["dimensions"]["fear"]["neutral"] == 0.1
    assert fingerprint(resolved) != default_fingerprint()


def test_custom_emotions_version_mismatch_rejected(tmp_path):
    base = emotions.default_emotions()
    base["emotion_version"] = "custom-1"
    path = tmp_path / "emotions.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    cfg = AffectConfig(emotions_path=str(path), expected_emotion_version="other")
    with pytest.raises(ValueError, match="version mismatch"):
        resolve_emotions(cfg)


def test_missing_emotions_file_errors(tmp_path):
    cfg = AffectConfig(emotions_path=str(tmp_path / "missing.json"))
    with pytest.raises(ValueError, match="emotions file not found"):
        resolve_emotions(cfg)


def test_config_json_custom_emotions_affects_engine(tmp_path):
    base = emotions.default_emotions()
    base["emotion_version"] = "0.2.0-custom"
    base["dimensions"]["fear"]["neutral"] = 0.25
    emotions_path = tmp_path / "emotions.json"
    emotions_path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")

    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "data_dir": "data",
                "emotions": {
                    "path": str(emotions_path),
                    "expected_version": "0.2.0-custom",
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(config)
    service = CompanionService(cfg)
    assert service.affect.initial_state()["base"]["fear"] == 0.25
    assert service.affect.emotions_version == "0.2.0-custom"
    service.close()


def test_engine_override_maps_merge_and_change_fingerprint(tmp_path):
    cfg = AppConfig(
        data_dir=tmp_path,
        affect=AffectConfig(dimensions={"fear": {"neutral": 0.05}}),
    )
    service = CompanionService(cfg)
    assert service.affect.spec["fear"]["neutral"] == 0.05
    assert service.affect.initial_state()["base"]["fear"] == 0.05
    assert service.affect.emotions_fingerprint != emotions.default_fingerprint()
    service.close()

    cfg = AppConfig(
        data_dir=tmp_path,
        affect=AffectConfig(label_patterns={"hostile": ["hate you", "custom phrase"]}),
    )
    service = CompanionService(cfg)
    assert service.affect.classify("custom phrase") == "hostile"
    assert service.affect.classify("i hate you") == "hostile"
    assert service.affect.classify("i am scared") == "fear_general"
    service.close()


def test_engine_rejects_unknown_override_dimension(tmp_path):
    from companion_gateway.affect import AffectEngine
    from companion_gateway.database import Database

    database = Database(tmp_path / "state.sqlite3")
    with pytest.raises(ValueError, match="unknown dimension"):
        AffectEngine(database, AffectConfig(dimensions={"bogus": {"neutral": 0.1}}))