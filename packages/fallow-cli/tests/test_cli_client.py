"""Unit tests for AdminClient against httpx.MockTransport (no real network)."""

from __future__ import annotations

import httpx
import pytest
from cli_helpers import (
    COORD_URL,
    make_transport,
    raising_transport,
    sample_agent,
    sample_job,
    sample_manifest,
)

from fallow_cli.client import AdminClient
from fallow_cli.errors import EXIT_AUTH, CliError
from fallow_protocol import JobSubmit, WorkerKind


def _client(transport: httpx.MockTransport) -> AdminClient:
    http = httpx.Client(base_url=COORD_URL, transport=transport)
    return AdminClient(http, "secret")


def test_create_enrollment_token() -> None:
    routes = {("POST", "/v1/admin/enrollment_tokens"): (201, {"token": "tok-1"})}
    with _client(make_transport(routes)) as client:
        assert client.create_enrollment_token() == "tok-1"


def test_create_api_key_omits_none_allowlist() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"key": "k-1"})

    with _client(httpx.MockTransport(handler)) as client:
        assert client.create_api_key("ci", None) == "k-1"
    assert "model_allowlist" not in seen["body"]  # type: ignore[operator]


def test_list_agents_parses_snapshots() -> None:
    routes = {("GET", "/v1/admin/agents"): (200, [sample_agent().model_dump(mode="json")])}
    with _client(make_transport(routes)) as client:
        agents = client.list_agents()
    assert agents[0].agent_id == "agent-1"


def test_list_models_parses_manifests() -> None:
    routes = {("GET", "/v1/admin/models"): (200, [sample_manifest().model_dump(mode="json")])}
    with _client(make_transport(routes)) as client:
        models = client.list_models()
    assert models[0].model_id == "qwen"


def test_register_model_requires_201() -> None:
    routes = {("POST", "/v1/admin/models"): (200, None)}  # wrong status
    with _client(make_transport(routes)) as client, pytest.raises(CliError):
        client.register_model(sample_manifest(), "/blobs/qwen.gguf")


def test_set_assignments_accepts_204() -> None:
    routes = {("PUT", "/v1/admin/assignments"): (204, None)}
    with _client(make_transport(routes)) as client:
        client.set_assignments("qwen", ("agent-1", "agent-2"))


def test_submit_job_returns_status() -> None:
    routes = {("POST", "/v1/admin/jobs"): (200, sample_job().model_dump(mode="json"))}
    job = JobSubmit(kind=WorkerKind.EMBED, model_id="qwen", payload_ref="ref")
    with _client(make_transport(routes)) as client:
        status = client.submit_job(job)
    assert status.job_id == "job-1"


def test_get_job_status() -> None:
    routes = {("GET", "/v1/admin/jobs/job-1"): (200, sample_job().model_dump(mode="json"))}
    with _client(make_transport(routes)) as client:
        assert client.get_job("job-1").total_units == 10


def test_401_maps_to_admin_key_rejected() -> None:
    routes = {("GET", "/v1/admin/agents"): (401, {"detail": "nope"})}
    with _client(make_transport(routes)) as client, pytest.raises(CliError) as exc:
        client.list_agents()
    assert exc.value.message == "admin key rejected"
    assert exc.value.exit_code == EXIT_AUTH


def test_connect_error_maps_to_unreachable() -> None:
    with _client(raising_transport()) as client, pytest.raises(CliError) as exc:
        client.list_agents()
    assert "coordinator unreachable at" in exc.value.message
    assert COORD_URL in exc.value.message


def test_generic_http_error_includes_detail() -> None:
    routes = {("GET", "/v1/admin/agents"): (500, {"detail": "boom"})}
    with _client(make_transport(routes)) as client, pytest.raises(CliError) as exc:
        client.list_agents()
    assert "500" in exc.value.message
    assert "boom" in exc.value.message
