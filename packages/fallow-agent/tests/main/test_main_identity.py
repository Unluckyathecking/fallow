"""First-run enrollment: register + persist (0600), then load-and-skip."""

from __future__ import annotations

import stat
from pathlib import Path

import httpx
import pytest
from main_helpers import make_settings, sample_caps

from fallow_agent.main import IdentityError, load_identity, resolve_identity
from fallow_protocol.messages import AgentConfig, RegisterResponse

AGENT_ID = "agent-42"
DEVICE_TOKEN = "dev-tok-abc"


class _Recorder:
    def __init__(self) -> None:
        self.register_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents/register":
            self.register_calls += 1
            body = RegisterResponse(
                agent_id=AGENT_ID,
                device_token=DEVICE_TOKEN,
                config=AgentConfig(heartbeat_interval_s=7.0),
            )
            return httpx.Response(200, content=body.model_dump_json())
        return httpx.Response(404)


def _client(recorder: _Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))


async def test_first_run_registers_and_persists_0600(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    recorder = _Recorder()
    identity, config = await resolve_identity(settings, _client(recorder), caps_factory=sample_caps)

    assert recorder.register_calls == 1
    assert identity.agent_id == AGENT_ID
    assert identity.device_token == DEVICE_TOKEN
    assert config.heartbeat_interval_s == 7.0  # came from RegisterResponse

    state_file = settings.state_path
    assert state_file.exists()
    mode = stat.S_IMODE(state_file.stat().st_mode)
    assert mode == 0o600  # owner read/write only

    persisted = load_identity(state_file)
    assert persisted is not None
    assert persisted.device_token == DEVICE_TOKEN


async def test_second_run_loads_and_skips_register(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    recorder = _Recorder()
    await resolve_identity(settings, _client(recorder), caps_factory=sample_caps)
    assert recorder.register_calls == 1

    # A fresh resolve against the same state_path must NOT hit the coordinator.
    second = _Recorder()
    identity, config = await resolve_identity(settings, _client(second), caps_factory=sample_caps)
    assert second.register_calls == 0
    assert identity.agent_id == AGENT_ID
    assert config.heartbeat_interval_s == AgentConfig().heartbeat_interval_s  # default


async def test_unenrolled_without_token_errors(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, enrollment_token=None)
    recorder = _Recorder()
    with pytest.raises(IdentityError, match="enrollment_token"):
        await resolve_identity(settings, _client(recorder), caps_factory=sample_caps)
    assert recorder.register_calls == 0
