from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from dusha import api
from dusha.config import HarnessConfig

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "integrations" / "agy_dusha"
HOOK_SCRIPT = PLUGIN_DIR / "hook.py"


def _run_hook(
    payload: dict[str, Any], env: dict[str, str | None]
) -> tuple[subprocess.CompletedProcess[str], Any]:
    merged = dict(os.environ)
    for key, value in env.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        cwd=str(PLUGIN_DIR),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=merged,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result, json.loads(result.stdout)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _search(client: httpx.Client, query: str) -> list[dict[str, Any]]:
    response = client.post(
        "/state/v1/memory/search",
        json={"query": query, "limit": 50, "context_messages": 0},
    )
    assert response.status_code == 200, response.text
    messages = [msg for hit in response.json()["results"] for msg in hit["messages"]]
    return sorted(messages, key=lambda item: item["id"])


def test_agy_plugin_manifest_and_multi_turn_lifecycle(config, run_app, tmp_path):
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    hooks = json.loads((PLUGIN_DIR / "hooks.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "dusha"
    assert manifest["version"]
    assert [item["command"] for item in hooks["dusha"]["PreInvocation"]] == ["python3 ./hook.py"]
    assert [item["command"] for item in hooks["dusha"]["Stop"]] == ["python3 ./hook.py"]

    identity = tmp_path / "identity.md"
    identity.write_text("You are Alice.", encoding="utf-8")
    config.plugin("identity").path = str(identity)
    config.api_token = "secret-agy-token"

    with run_app(api.create_app(config)) as base_url:
        client = httpx.Client(base_url=base_url, headers={"X-Companion-Token": "secret-agy-token"})
        fact = client.post(
            "/state/v1/evergreen/facts",
            json={"key": "user.editor", "text": "Prefers Helix."},
        )
        assert fact.status_code == 200, fact.text

        env = {"DUSHA_URL": base_url, "DUSHA_TOKEN": "secret-agy-token"}
        transcript = tmp_path / "transcript.jsonl"
        conversation_id = "conv-agy-1"

        rows = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-20T08:00:00Z",
                "content": "Remember the amber window.",
            }
        ]
        _write_jsonl(transcript, rows)

        pre_event = {
            "hookEventName": "PreInvocation",
            "conversationId": conversation_id,
            "lastUserInput": "Remember the amber window.",
            "transcriptPath": str(transcript),
            "invocationNum": 1,
            "initialNumSteps": 1,
        }
        _, first_output = _run_hook(pre_event, env)
        steps = first_output["injectSteps"]
        assert len(steps) == 1
        injected = steps[0]["systemMessage"]["systemMessage"]
        assert "You are Alice." in injected
        assert "Prefers Helix." in injected
        assert "<companion_state>" in injected

        _, second_output = _run_hook({**pre_event, "invocationNum": 2}, env)
        assert second_output == {}
        user_hits = _search(client, "amber window")
        assert [(item["role"], item["harness"], item["text"]) for item in user_hits] == [
            ("user", "agy", "Remember the amber window.")
        ]

        rows.extend(
            [
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-20T08:00:01Z",
                    "thinking": "internal thoughts must not be archived",
                    "tool_calls": [{"name": "read_file", "args": {"Path": "notes.md"}}],
                },
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-11-20T08:00:02Z",
                    "content": "tool output",
                },
                {
                    "step_index": 3,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-20T08:00:03Z",
                    "content": "Noted the cobalt lantern by the amber window.",
                },
            ]
        )
        _write_jsonl(transcript, rows)

        stop_event = {
            "hookEventName": "Stop",
            "conversationId": conversation_id,
            "transcriptPath": str(transcript),
            "terminationReason": "NO_TOOL_CALL",
        }
        _, stop_output = _run_hook(stop_event, env)
        assert stop_output == {}
        _, repeat_stop = _run_hook(stop_event, env)
        assert repeat_stop == {}

        reply_hits = _search(client, "cobalt lantern")
        assert [(item["role"], item["harness"], item["text"]) for item in reply_hits] == [
            ("assistant", "agy", "Noted the cobalt lantern by the amber window.")
        ]
        assert _search(client, "internal thoughts") == []

        transcript2 = tmp_path / "transcript2.jsonl"
        transcript2_full = tmp_path / "transcript_full.jsonl"
        conv2_id = "conv-agy-2"
        conv2_rows = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-20T08:01:00Z",
                "content": "Where was the cobalt lantern?",
            }
        ]
        _write_jsonl(transcript2, conv2_rows)
        _, turn2_output = _run_hook(
            {
                "hookEventName": "PreInvocation",
                "conversationId": conv2_id,
                "lastUserInput": "Where was the cobalt lantern?",
                "transcriptPath": str(transcript2),
                "invocationNum": 1,
                "initialNumSteps": 1,
            },
            env,
        )
        turn2_injected = turn2_output["injectSteps"][0]["systemMessage"]["systemMessage"]
        assert "Noted the cobalt lantern by the amber window." in turn2_injected

        full_reply = "The cobalt lantern sits beside the amber window with full untruncated detail."
        truncated_row = {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-20T08:01:05Z",
            "content": "The cobalt lantern sits...\n<truncated>",
            "truncated_fields": ["content"],
        }
        full_row = {**truncated_row, "content": full_reply}
        full_row.pop("truncated_fields")
        _write_jsonl(transcript2, [*conv2_rows, truncated_row])
        _write_jsonl(transcript2_full, [*conv2_rows, full_row])

        _, stop2_output = _run_hook(
            {
                "hookEventName": "Stop",
                "conversationId": conv2_id,
                "transcriptPath": str(transcript2),
                "terminationReason": "NO_TOOL_CALL",
            },
            env,
        )
        assert stop2_output == {}
        full_hits = _search(client, "untruncated detail")
        assert [(item["role"], item["text"]) for item in full_hits] == [("assistant", full_reply)]
        client.close()


def test_agy_hook_discovers_companion_config_and_applies_harness_filters(config, run_app, tmp_path):
    config.api_token = "auto-discovered-token"
    config.harnesses["agy"] = HarnessConfig(exclude=("affect",))
    identity = tmp_path / "identity.md"
    identity.write_text("You are Clara.", encoding="utf-8")
    config.plugin("identity").path = str(identity)

    with run_app(api.create_app(config)) as base_url:
        port = int(base_url.rsplit(":", 1)[1])
        xdg = tmp_path / "xdg"
        companion_dir = xdg / "dusha" / "clara"
        companion_dir.mkdir(parents=True)
        (companion_dir / "config.json").write_text(
            json.dumps({"host": "127.0.0.1", "port": port, "api_token": "auto-discovered-token"}),
            encoding="utf-8",
        )

        clean_env: dict[str, str | None] = {"XDG_CONFIG_HOME": str(xdg)}
        for key in ("DUSHA_URL", "DUSHA_TOKEN", "DUSHA_COMPANION", "DUSHA_HOME", "DUSHA_CONFIG"):
            clean_env[key] = None

        _, output = _run_hook(
            {
                "hookEventName": "PreInvocation",
                "conversationId": "conv-auto",
                "lastUserInput": "Check the garden.",
                "initialNumSteps": 1,
            },
            clean_env,
        )
        injected = output["injectSteps"][0]["systemMessage"]["systemMessage"]
        assert "You are Clara." in injected
        assert "<affect>" not in injected

        _, repeated = _run_hook(
            {
                "hookEventName": "PreInvocation",
                "conversationId": "conv-auto",
                "lastUserInput": "Check the garden.",
                "initialNumSteps": 1,
            },
            clean_env,
        )
        assert repeated == {}

        _, stripped = _run_hook(
            {
                "hookEventName": "PreInvocation",
                "conversationId": "conv-auto",
                "lastUserInput": "Check the greenhouse.",
                "initialNumSteps": 4,
            },
            {**clean_env, "DUSHA_PLUGINS": "none"},
        )
        stripped_text = stripped["injectSteps"][0]["systemMessage"]["systemMessage"]
        assert "You are Clara." not in stripped_text
        assert "<companion_state>" in stripped_text


def test_agy_hook_extracts_wrapped_user_input_and_fails_open(config, run_app, tmp_path, free_port):
    config.api_token = "strict-token"
    transcript = tmp_path / "wrapped.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": (
                    "<USER_REQUEST>\n<system_instructions>hidden</system_instructions>"
                    "Find the silver compass.\n</USER_REQUEST>"
                ),
            }
        ],
    )

    with run_app(api.create_app(config)) as base_url:
        client = httpx.Client(base_url=base_url, headers={"X-Companion-Token": "strict-token"})
        _, output = _run_hook(
            {
                "hookEventName": "PreInvocation",
                "conversationId": "conv-wrapped",
                "lastUserInput": "",
                "transcriptPath": str(transcript),
            },
            {"DUSHA_URL": base_url, "DUSHA_TOKEN": "strict-token"},
        )
        assert "injectSteps" in output
        hits = _search(client, "silver compass")
        assert [item["text"] for item in hits] == ["Find the silver compass."]
        client.close()

        unauthorized, unauth_output = _run_hook(
            {
                "hookEventName": "PreInvocation",
                "conversationId": "conv-unauth",
                "lastUserInput": "Should fail open.",
                "initialNumSteps": 1,
            },
            {"DUSHA_URL": base_url, "DUSHA_TOKEN": "wrong-token"},
        )
        assert unauth_output == {}
        assert "[dusha]" in unauthorized.stderr

    dead_port = free_port()
    offline, offline_output = _run_hook(
        {
            "hookEventName": "PreInvocation",
            "conversationId": "conv-offline",
            "lastUserInput": "Server is down.",
            "initialNumSteps": 1,
        },
        {"DUSHA_URL": f"http://127.0.0.1:{dead_port}", "DUSHA_TIMEOUT": "1"},
    )
    assert offline_output == {}
    assert "[dusha]" in offline.stderr
