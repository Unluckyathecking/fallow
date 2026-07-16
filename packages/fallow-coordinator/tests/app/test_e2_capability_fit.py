"""Capability-aware assignment: reject models that will not fit, and the /fit probe."""

from __future__ import annotations

from pathlib import Path

import httpx
from app_helpers import (
    MODEL_ID,
    START,
    Harness,
    admin_headers,
    bearer,
    enrolled_idle_agent,
    make_manifest,
    send_heartbeat,
)

from fallow_protocol.capabilities import GpuStatus
from fallow_protocol.messages import AgentState, Heartbeat
from fallow_protocol.models import ModelManifest
from fallow_protocol.version import PROTOCOL_VERSION


async def _heartbeat(
    client: httpx.AsyncClient,
    agent_id: str,
    device_token: str,
    *,
    mem_available_mb: int = 8192,
    vram_free_mb: int | None = None,
) -> httpx.Response:
    """Post a heartbeat with explicit memory and (optional) single-GPU VRAM."""
    gpus = (
        ()
        if vram_free_mb is None
        else (GpuStatus(index=0, vram_free_mb=vram_free_mb, util_percent=0.0),)
    )
    hb = Heartbeat(
        agent_id=agent_id,
        seq=2,
        sent_at=START,
        protocol_version=PROTOCOL_VERSION,
        state=AgentState.IDLE,
        user_idle_s=300.0,
        cpu_percent=5.0,
        mem_available_mb=mem_available_mb,
        gpus=gpus,
    )
    return await client.post(
        f"/v1/agents/{agent_id}/heartbeat",
        json=hb.model_dump(mode="json"),
        headers=bearer(device_token),
    )


async def _register_model(harness: Harness, tmp_path: Path, manifest: ModelManifest) -> None:
    blob = tmp_path / f"{manifest.model_id}.gguf"
    blob.write_bytes(b"fake-gguf-bytes")
    resp = await harness.client.post(
        "/v1/admin/models",
        json={"manifest": manifest.model_dump(mode="json"), "blob_path": str(blob)},
        headers=admin_headers(),
    )
    assert resp.status_code == 201, resp.text


async def _assign(harness: Harness, model_id: str, agent_id: str) -> int:
    resp = await harness.client.put(
        "/v1/admin/assignments",
        json={"model_id": model_id, "agent_ids": [agent_id]},
        headers=admin_headers(),
    )
    return resp.status_code


async def test_assignment_rejects_model_that_does_not_fit(harness: Harness, tmp_path: Path) -> None:
    # The default agent reports no GPU, so a VRAM-hungry model cannot fit.
    manifest = make_manifest().model_copy(update={"min_vram_mb": 8000})
    await _register_model(harness, tmp_path, manifest)
    agent_id, _ = await enrolled_idle_agent(harness.client)

    resp = await harness.client.put(
        "/v1/admin/assignments",
        json={"model_id": MODEL_ID, "agent_ids": [agent_id]},
        headers=admin_headers(),
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert MODEL_ID in detail
    assert agent_id in detail
    assert "8000 MB VRAM" in detail  # required
    assert "0 MB VRAM" in detail  # available


async def test_fitting_assignment_succeeds(harness: Harness, tmp_path: Path) -> None:
    # The default agent reports 8192 MB available RAM and no VRAM need.
    manifest = make_manifest().model_copy(update={"min_ram_mb": 4096})
    await _register_model(harness, tmp_path, manifest)
    agent_id, device_token = await enrolled_idle_agent(harness.client)

    assert await _assign(harness, MODEL_ID, agent_id) == 204
    hb = await send_heartbeat(harness.client, agent_id, device_token)
    assert hb.json()["desired_models"] == [MODEL_ID]


async def test_unregistered_model_is_not_fit_checked(harness: Harness) -> None:
    # No model registered: the fit check is skipped and the assignment applies.
    agent_id, _ = await enrolled_idle_agent(harness.client)
    assert await _assign(harness, MODEL_ID, agent_id) == 204


async def test_fit_endpoint_reports_true_and_false(harness: Harness, tmp_path: Path) -> None:
    fitting = make_manifest(model_id="small").model_copy(update={"min_ram_mb": 4096})
    too_big = make_manifest(model_id="huge").model_copy(update={"min_vram_mb": 8000})
    await _register_model(harness, tmp_path, fitting)
    await _register_model(harness, tmp_path, too_big)
    agent_id, _ = await enrolled_idle_agent(harness.client)

    ok = await harness.client.get(
        f"/v1/admin/agents/{agent_id}/fit",
        params={"model_id": "small"},
        headers=admin_headers(),
    )
    assert ok.status_code == 200
    assert ok.json() == {
        "fits": True,
        "required_vram_mb": 0,
        "required_ram_mb": 4096,
        "available_vram_mb": 0,
        "available_ram_mb": 8192,
    }

    bad = await harness.client.get(
        f"/v1/admin/agents/{agent_id}/fit",
        params={"model_id": "huge"},
        headers=admin_headers(),
    )
    assert bad.status_code == 200
    body = bad.json()
    assert body["fits"] is False
    assert body["required_vram_mb"] == 8000
    assert body["available_vram_mb"] == 0


async def test_fit_endpoint_unknown_model_or_agent_is_404(harness: Harness, tmp_path: Path) -> None:
    agent_id, _ = await enrolled_idle_agent(harness.client)
    unknown_model = await harness.client.get(
        f"/v1/admin/agents/{agent_id}/fit",
        params={"model_id": "nope"},
        headers=admin_headers(),
    )
    assert unknown_model.status_code == 404

    await _register_model(harness, tmp_path, make_manifest())
    unknown_agent = await harness.client.get(
        "/v1/admin/agents/ghost/fit",
        params={"model_id": MODEL_ID},
        headers=admin_headers(),
    )
    assert unknown_agent.status_code == 404


async def test_fit_endpoint_requires_admin(harness: Harness) -> None:
    resp = await harness.client.get(
        "/v1/admin/agents/whoever/fit",
        params={"model_id": MODEL_ID},
        headers={"Authorization": "Bearer nope"},
    )
    assert resp.status_code == 401


async def test_reassert_does_not_reject_an_already_serving_agent(
    harness: Harness, tmp_path: Path
) -> None:
    # A GPU agent takes a VRAM-hungry model while it has room, then reports less
    # free VRAM once the model is loaded. Re-asserting the same mapping must not
    # start rejecting a model the agent is already serving.
    manifest = make_manifest().model_copy(update={"min_vram_mb": 8000})
    await _register_model(harness, tmp_path, manifest)
    agent_id, device_token = await enrolled_idle_agent(harness.client)
    await _heartbeat(harness.client, agent_id, device_token, vram_free_mb=12000)

    assert await _assign(harness, MODEL_ID, agent_id) == 204

    # The loaded model has consumed VRAM: free drops below the requirement.
    await _heartbeat(harness.client, agent_id, device_token, vram_free_mb=2000)
    assert await _assign(harness, MODEL_ID, agent_id) == 204


async def test_mixed_request_rejects_all_or_nothing(harness: Harness, tmp_path: Path) -> None:
    manifest = make_manifest().model_copy(update={"min_vram_mb": 8000})
    await _register_model(harness, tmp_path, manifest)
    fits_id, fits_token = await enrolled_idle_agent(harness.client)
    await _heartbeat(harness.client, fits_id, fits_token, vram_free_mb=12000)
    unfit_id, unfit_token = await enrolled_idle_agent(harness.client)  # no GPU

    resp = await harness.client.put(
        "/v1/admin/assignments",
        json={"model_id": MODEL_ID, "agent_ids": [fits_id, unfit_id]},
        headers=admin_headers(),
    )
    assert resp.status_code == 409
    assert unfit_id in resp.json()["detail"]

    # All-or-nothing: the fitting agent must not have been written either.
    fitting_hb = await _heartbeat(harness.client, fits_id, fits_token, vram_free_mb=12000)
    assert fitting_hb.json()["desired_models"] == []
    unfit_hb = await send_heartbeat(harness.client, unfit_id, unfit_token)
    assert unfit_hb.json()["desired_models"] == []
