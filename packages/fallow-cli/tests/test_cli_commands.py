"""End-to-end command tests via typer's CliRunner + injected MockTransport."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cli_helpers import (
    COORD_URL,
    bytes_transport,
    make_transport,
    raising_transport,
    recording_transport,
    sample_agent,
    sample_job,
    sample_manifest,
)
from pytest import MonkeyPatch
from typer.testing import CliRunner

from fallow_cli import blobs, main


def _invoke(
    runner: CliRunner, env: dict[str, str], args: list[str], *, as_json: bool = False
) -> object:
    prefix = ["--coordinator-url", COORD_URL]
    if as_json:
        prefix.append("--json")
    return runner.invoke(main.app, [*prefix, *args], env=env)


def _use_admin(monkeypatch: MonkeyPatch, routes: object) -> None:
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", make_transport(routes))  # type: ignore[arg-type]


# ── Happy paths ──────────────────────────────────────────────────────────────
def test_enroll_new_token(runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch) -> None:
    _use_admin(monkeypatch, {("POST", "/v1/admin/enrollment_tokens"): (201, {"token": "tok-1"})})
    result = _invoke(runner, env, ["enroll", "new-token"])
    assert result.exit_code == 0
    assert "tok-1" in result.output


def test_keys_new_with_allowlist(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch
) -> None:
    _use_admin(monkeypatch, {("POST", "/v1/admin/api_keys"): (200, {"key": "k9"})})
    result = _invoke(runner, env, ["keys", "new", "ci-bot", "--allow", "qwen, llama"])
    assert result.exit_code == 0
    assert "k9" in result.output


def test_keys_new_round_trips_quota_options(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch
) -> None:
    store: dict[str, object] = {}
    monkeypatch.setattr(
        main,
        "_ADMIN_TRANSPORT",
        recording_transport(store, status=201, response_body={"key": "k-limited"}),
    )
    result = _invoke(
        runner,
        env,
        ["keys", "new", "limited", "--rpm", "10", "--per-day", "250"],
    )
    assert result.exit_code == 0
    assert store["path"] == "/v1/admin/api_keys"
    assert store["body"] == {"name": "limited", "rpm_limit": 10, "daily_limit": 250}


def test_agents_list_table_and_json(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch
) -> None:
    routes = {("GET", "/v1/admin/agents"): (200, [sample_agent().model_dump(mode="json")])}
    _use_admin(monkeypatch, routes)
    table = _invoke(runner, env, ["agents", "list"])
    assert table.exit_code == 0
    assert "agent-1" in table.output

    _use_admin(monkeypatch, routes)
    js = _invoke(runner, env, ["agents", "list"], as_json=True)
    assert js.exit_code == 0
    payload = json.loads(js.output)
    assert payload[0]["agent_id"] == "agent-1"


def test_models_list_table_and_json(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch
) -> None:
    routes = {("GET", "/v1/admin/models"): (200, [sample_manifest().model_dump(mode="json")])}
    _use_admin(monkeypatch, routes)
    table = _invoke(runner, env, ["models", "list"])
    assert table.exit_code == 0
    assert "qwen" in table.output

    _use_admin(monkeypatch, routes)
    js = _invoke(runner, env, ["models", "list"], as_json=True)
    assert json.loads(js.output)[0]["model_id"] == "qwen"


def test_models_register_computes_sha256(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    blob = tmp_path / "weights.gguf"
    payload = b"weight-bytes" * 10000
    blob.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    store: dict[str, object] = {}
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", recording_transport(store, status=201))

    result = _invoke(
        runner,
        env,
        [
            "models",
            "register",
            "--file",
            str(blob),
            "--model-id",
            "m",
            "--family",
            "f",
            "--quant",
            "Q4",
        ],
    )
    assert result.exit_code == 0, result.output
    body = store["body"]
    assert isinstance(body, dict)
    assert body["manifest"]["sha256"] == expected
    assert body["manifest"]["size_bytes"] == len(payload)
    assert Path(str(body["blob_path"])).is_absolute()


def test_models_pull_downloads_then_registers(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    payload = b"downloaded-weights" * 5000
    expected = hashlib.sha256(payload).hexdigest()
    store: dict[str, object] = {}
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(main, "_DOWNLOAD_TRANSPORT", bytes_transport(payload))
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", recording_transport(store, status=201))

    result = _invoke(
        runner,
        env,
        [
            "models",
            "pull",
            "http://host/qwen.gguf",
            "--model-id",
            "qwen",
            "--family",
            "q",
            "--quant",
            "Q4",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "blobs" / "qwen.gguf").read_bytes() == payload
    body = store["body"]
    assert isinstance(body, dict)
    assert body["manifest"]["sha256"] == expected
    assert body["manifest"]["source_url"] == "http://host/qwen.gguf"


def test_assign(runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch) -> None:
    store: dict[str, object] = {}
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", recording_transport(store, status=204))
    result = _invoke(runner, env, ["assign", "qwen", "agent-1", "agent-2"])
    assert result.exit_code == 0
    body = store["body"]
    assert isinstance(body, dict)
    assert body["agent_ids"] == ["agent-1", "agent-2"]


def test_jobs_submit(runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch) -> None:
    routes = {("POST", "/v1/admin/jobs"): (200, sample_job().model_dump(mode="json"))}
    _use_admin(monkeypatch, routes)
    result = _invoke(
        runner,
        env,
        ["jobs", "submit", "--kind", "embed", "--model-id", "qwen", "--payload-ref", "corpus"],
    )
    assert result.exit_code == 0
    assert "job-1" in result.output


def test_jobs_status(runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch) -> None:
    routes = {("GET", "/v1/admin/jobs/job-1"): (200, sample_job().model_dump(mode="json"))}
    _use_admin(monkeypatch, routes)
    result = _invoke(runner, env, ["jobs", "status", "job-1"])
    assert result.exit_code == 0
    assert "running" in result.output.lower()


def test_status_table_and_json(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch
) -> None:
    routes = {
        ("GET", "/v1/admin/agents"): (200, [sample_agent().model_dump(mode="json")]),
        ("GET", "/v1/admin/models"): (200, [sample_manifest().model_dump(mode="json")]),
    }
    _use_admin(monkeypatch, routes)
    table = _invoke(runner, env, ["status"])
    assert table.exit_code == 0
    assert "agents" in table.output

    _use_admin(monkeypatch, routes)
    js = _invoke(runner, env, ["status"], as_json=True)
    payload = json.loads(js.output)
    assert payload["agents"][0]["agent_id"] == "agent-1"
    assert payload["models"][0]["model_id"] == "qwen"


# ── Failure paths ────────────────────────────────────────────────────────────
def test_auth_failure_message_and_exit_code(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch
) -> None:
    _use_admin(monkeypatch, {("GET", "/v1/admin/agents"): (401, {"detail": "nope"})})
    result = _invoke(runner, env, ["agents", "list"])
    assert result.exit_code == 2
    assert "admin key rejected" in result.output


def test_unreachable_coordinator_message(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", raising_transport())
    result = _invoke(runner, env, ["agents", "list"])
    assert result.exit_code == 1
    assert "coordinator unreachable at" in result.output
    assert COORD_URL in result.output


def test_missing_admin_key_exit_code(runner: CliRunner, tmp_path: Path) -> None:
    env = {"FLW_CONFIG_FILE": str(tmp_path / "absent.toml")}
    result = _invoke(runner, env, ["agents", "list"])
    assert result.exit_code == 2
    assert "admin key" in result.output.lower()


def test_config_precedence_flag_over_env(
    runner: CliRunner, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    # env points at a different (unreachable) URL; the flag must win, so the
    # unreachable message names the FLAG url, not the env one.
    env = {
        "FLW_ADMIN_KEY": "secret",
        "FLW_CONFIG_FILE": str(tmp_path / "absent.toml"),
        "FLW_COORDINATOR_URL": "http://env-url",
    }
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", raising_transport())
    result = _invoke(runner, env, ["agents", "list"])
    assert COORD_URL in result.output
    assert "env-url" not in result.output
