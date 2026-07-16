"""Auto model selection on enroll (ADR 048): the opt-in placement seam.

Enroll fits against the machine's declared caps (no heartbeat yet), so these
tests drive the real register endpoint and read the resulting assignment back
through the next heartbeat's ``desired_models``.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from app_helpers import (
    Harness,
    admin_headers,
    make_caps,
    make_manifest,
    make_register_request,
    mint_enrollment_token,
    register_agent,
    send_heartbeat,
)

from fallow_coordinator.app.agent_routes import _auto_assign_on_enroll
from fallow_protocol.models import ModelManifest


async def _register_model(
    client: httpx.AsyncClient, tmp_path: Path, manifest: ModelManifest
) -> None:
    blob = tmp_path / f"{manifest.model_id}.gguf"
    blob.write_bytes(b"fake-gguf-bytes")
    resp = await client.post(
        "/v1/admin/models",
        json={"manifest": manifest.model_dump(mode="json"), "blob_path": str(blob)},
        headers=admin_headers(),
    )
    assert resp.status_code == 201, resp.text


async def _enroll_and_read_desired(harness: Harness) -> list[str]:
    """Enroll a default (no-GPU, 16384 MB RAM) agent, then read its assignment."""
    token = await mint_enrollment_token(harness.client)
    agent_id, device_token = await register_agent(harness.client, token)
    hb = await send_heartbeat(harness.client, agent_id, device_token)
    return list(hb.json()["desired_models"])


async def test_enroll_auto_assigns_the_largest_fitting_model(
    harness_auto_assign: Harness, tmp_path: Path
) -> None:
    big = make_manifest(model_id="big").model_copy(update={"size_bytes": 8000, "min_ram_mb": 4096})
    small = make_manifest(model_id="small").model_copy(
        update={"size_bytes": 1000, "min_ram_mb": 1024}
    )
    await _register_model(harness_auto_assign.client, tmp_path, big)
    await _register_model(harness_auto_assign.client, tmp_path, small)

    assert await _enroll_and_read_desired(harness_auto_assign) == ["big"]


async def test_enroll_assigns_nothing_when_no_model_fits(
    harness_auto_assign: Harness, tmp_path: Path
) -> None:
    # A no-GPU agent cannot hold a VRAM-hungry model, so nothing is assigned and
    # the enroll still succeeds.
    gpu_only = make_manifest(model_id="gpu-only").model_copy(update={"min_vram_mb": 8000})
    await _register_model(harness_auto_assign.client, tmp_path, gpu_only)

    assert await _enroll_and_read_desired(harness_auto_assign) == []


async def test_flag_off_never_auto_assigns(harness: Harness, tmp_path: Path) -> None:
    fitting = make_manifest(model_id="fits").model_copy(update={"min_ram_mb": 1024})
    await _register_model(harness.client, tmp_path, fitting)

    assert await _enroll_and_read_desired(harness) == []


async def test_existing_assignment_is_left_untouched(
    harness_auto_assign: Harness, tmp_path: Path
) -> None:
    # First enroll assigns the largest fitting model. A later, larger model must
    # not bump an agent that already has an assignment.
    big = make_manifest(model_id="big").model_copy(update={"size_bytes": 8000, "min_ram_mb": 1024})
    await _register_model(harness_auto_assign.client, tmp_path, big)
    token = await mint_enrollment_token(harness_auto_assign.client)
    agent_id, _ = await register_agent(harness_auto_assign.client, token)
    assert await harness_auto_assign.state.registry.desired_models(agent_id) == ("big",)

    huge = make_manifest(model_id="huge").model_copy(
        update={"size_bytes": 99_000, "min_ram_mb": 1024}
    )
    await _register_model(harness_auto_assign.client, tmp_path, huge)
    await _auto_assign_on_enroll(harness_auto_assign.state, agent_id, make_caps())

    assert await harness_auto_assign.state.registry.desired_models(agent_id) == ("big",)


async def test_placement_failure_still_returns_the_device_token(
    harness_auto_assign: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The token is spent and the agent row committed before placement runs, so a
    # placement error must not fail the enroll — otherwise the single-use token
    # burns with no device_token ever returned and the machine can never join.
    fitting = make_manifest(model_id="fits").model_copy(update={"min_ram_mb": 1024})
    await _register_model(harness_auto_assign.client, tmp_path, fitting)

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated sqlite lock during placement")

    monkeypatch.setattr(harness_auto_assign.state.registry, "set_assignments", _boom)

    token = await mint_enrollment_token(harness_auto_assign.client)
    body = make_register_request(token).model_dump(mode="json")
    resp = await harness_auto_assign.client.post("/v1/agents/register", json=body)

    assert resp.status_code == 201, resp.text
    assert resp.json()["device_token"]
