"""End-to-end batch flow: submit → chunk → lease → input fetch → result → DONE."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from app_helpers import (
    MODEL_ID,
    Harness,
    admin_headers,
    bearer,
    enrolled_idle_agent,
    make_replica,
    make_success_result,
)

from fallow_coordinator.app import admin_routes
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import JobState, JobStatus, JobSubmit, WorkUnitLease


def _write_corpus(tmp: Path, n: int) -> Path:
    path = tmp / "corpus.jsonl"
    path.write_text(
        "\n".join(f'{{"id": "{i}", "text": "chunk {i}"}}' for i in range(n)), encoding="utf-8"
    )
    return path


async def _submit_embed(harness: Harness, corpus: Path) -> JobStatus:
    job = JobSubmit(kind=WorkerKind.EMBED, model_id=MODEL_ID, payload_ref=str(corpus))
    resp = await harness.client.post(
        "/v1/admin/jobs", json=job.model_dump(mode="json"), headers=admin_headers()
    )
    assert resp.status_code == 201, resp.text
    return JobStatus.model_validate(resp.json())


async def _lease(harness: Harness, agent_id: str, token: str) -> WorkUnitLease | None:
    resp = await harness.client.get(
        f"/v1/agents/{agent_id}/work", params={"timeout": 0}, headers=bearer(token)
    )
    if resp.status_code == 204:
        return None
    assert resp.status_code == 200, resp.text
    return WorkUnitLease.model_validate(resp.json())


async def _upload_and_complete(
    harness: Harness, agent_id: str, token: str, lease: WorkUnitLease
) -> None:
    headers = {**bearer(token), "X-Fallow-Lease-Attempt": str(lease.attempt)}
    uploaded = await harness.client.post(
        f"/v1/agents/{agent_id}/work_units/{lease.work_unit_id}/payload",
        content=b"stored result",
        headers=headers,
    )
    assert uploaded.status_code == 200, uploaded.text
    result = make_success_result(lease.work_unit_id).model_copy(
        update={"result_ref": uploaded.json()["result_ref"]}
    )
    done = await harness.client.post(
        f"/v1/agents/{agent_id}/work_units/{lease.work_unit_id}/result",
        json=result.model_dump(mode="json"),
        headers=headers,
    )
    assert done.status_code == 200, done.text


async def test_embed_chunked_into_expected_unit_count(
    harness_small_chunks: Harness, tmp_path: Path
) -> None:
    # chunks_per_unit=2, 5 texts → ceil(5/2) = 3 units.
    corpus = _write_corpus(tmp_path, n=5)
    status = await _submit_embed(harness_small_chunks, corpus)
    assert status.total_units == 3
    assert status.state == JobState.PENDING


async def test_full_job_flow_lease_input_result_done(
    harness_small_chunks: Harness, tmp_path: Path
) -> None:
    h = harness_small_chunks
    agent_id, token = await enrolled_idle_agent(h.client, replicas=(make_replica(),))
    corpus = _write_corpus(tmp_path, n=5)
    status = await _submit_embed(h, corpus)
    assert status.total_units == 3

    leased = 0
    while (lease := await _lease(h, agent_id, token)) is not None:
        leased += 1
        assert lease.model_id == MODEL_ID
        assert lease.kind == WorkerKind.EMBED

        # Input fetch round-trips against the content-addressed store.
        input_resp = await h.client.get(
            f"/v1/work_units/{lease.input_url}/input", headers=bearer(token)
        )
        assert input_resp.status_code == 200
        stored = (h.config.unit_input_dir / lease.input_url).read_bytes()
        assert input_resp.content == stored
        assert isinstance(json.loads(input_resp.content), list)

        # Upload and complete the unit successfully.
        await _upload_and_complete(h, agent_id, token, lease)

    assert leased == 3
    final = await h.client.get(f"/v1/admin/jobs/{status.job_id}", headers=admin_headers())
    final_status = JobStatus.model_validate(final.json())
    assert final_status.state == JobState.DONE
    assert final_status.done_units == 3
    records = [
        json.loads(line)
        for line in h.config.events_jsonl_path.with_name("units.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [record["state"] for record in records] == ["leased", "done"] * 3
    assert all(record["agent_id"] == agent_id for record in records)


async def test_ocr_job_leases_self_contained_page_units(harness: Harness, tmp_path: Path) -> None:
    h = harness
    agent_id, token = await enrolled_idle_agent(h.client, replicas=(make_replica(),))
    corpus = tmp_path / "pages"
    corpus.mkdir()
    for i in range(3):
        (corpus / f"{i:02d}.png").write_bytes(b"\x89PNG-fake-" + bytes([i]) * 8)

    job = JobSubmit(kind=WorkerKind.OCR, model_id=MODEL_ID, payload_ref=str(corpus))
    resp = await h.client.post(
        "/v1/admin/jobs", json=job.model_dump(mode="json"), headers=admin_headers()
    )
    assert resp.status_code == 201, resp.text
    status = JobStatus.model_validate(resp.json())
    assert status.total_units == 3

    lease = await _lease(h, agent_id, token)
    assert lease is not None
    assert lease.kind == WorkerKind.OCR

    input_resp = await h.client.get(
        f"/v1/work_units/{lease.input_url}/input", headers=bearer(token)
    )
    assert input_resp.status_code == 200
    unit = json.loads(input_resp.content)
    assert unit["schema"] == "ocr-unit/1"
    assert "image_b64" in unit

    await _upload_and_complete(h, agent_id, token, lease)


async def test_job_units_exposes_ids_and_result_refs(
    harness_small_chunks: Harness, tmp_path: Path
) -> None:
    """Operators join results back to their corpus through this listing."""
    h = harness_small_chunks
    agent_id, token = await enrolled_idle_agent(h.client, replicas=(make_replica(),))
    corpus = _write_corpus(tmp_path, n=5)
    status = await _submit_embed(h, corpus)
    while (lease := await _lease(h, agent_id, token)) is not None:
        await _upload_and_complete(h, agent_id, token, lease)

    resp = await h.client.get(f"/v1/admin/jobs/{status.job_id}/units", headers=admin_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == status.job_id
    assert body["model_id"] == MODEL_ID
    assert [unit["idx"] for unit in body["units"]] == [0, 1, 2]
    for unit in body["units"]:
        assert unit["state"] == "done"
        assert unit["result_status"] == "succeeded"
        assert unit["result_ref"]


async def test_job_units_unknown_job_is_404(harness: Harness) -> None:
    resp = await harness.client.get("/v1/admin/jobs/ghost/units", headers=admin_headers())
    assert resp.status_code == 404


async def test_input_fetch_unknown_ref_is_404(harness: Harness) -> None:
    _agent_id, token = await enrolled_idle_agent(harness.client)
    resp = await harness.client.get("/v1/work_units/deadbeef/input", headers=bearer(token))
    assert resp.status_code == 404


async def test_dedup_resubmit_is_instant_done(
    harness_small_chunks: Harness, tmp_path: Path
) -> None:
    h = harness_small_chunks
    agent_id, token = await enrolled_idle_agent(h.client, replicas=(make_replica(),))
    corpus = _write_corpus(tmp_path, n=5)
    await _submit_embed(h, corpus)

    # Drain + complete every unit of the first submission.
    while (lease := await _lease(h, agent_id, token)) is not None:
        await _upload_and_complete(h, agent_id, token, lease)

    # Re-submitting the identical corpus dedups to DONE immediately (same ids).
    resubmit = await _submit_embed(h, corpus)
    assert resubmit.state == JobState.DONE
    assert resubmit.done_units == resubmit.total_units == 3


async def test_unknown_payload_is_422(harness: Harness) -> None:
    job = JobSubmit(kind=WorkerKind.EMBED, model_id=MODEL_ID, payload_ref="/no/such/path")
    resp = await harness.client.post(
        "/v1/admin/jobs", json=job.model_dump(mode="json"), headers=admin_headers()
    )
    assert resp.status_code == 422


async def test_submit_chunking_does_not_block_event_loop(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large corpus is read and encoded off the event loop. While one submit is
    still splitting, a concurrent admin request is still served — the property the
    coordinator needs so a tens-of-thousands-page OCR submit cannot stall
    heartbeats and lease renewals."""
    h = harness
    corpus = tmp_path / "pages"
    corpus.mkdir()
    (corpus / "00.png").write_bytes(b"\x89PNG-fake-x")

    entered = threading.Event()
    release = threading.Event()
    real_chunk = admin_routes.chunk_job

    def blocking_chunk(*args: object, **kwargs: object) -> object:
        # Park the worker thread mid-split. If this ran on the event loop, the
        # concurrent GET below could not be served until it returned.
        entered.set()
        assert release.wait(timeout=5), "chunk was never released"
        return real_chunk(*args, **kwargs)

    monkeypatch.setattr(admin_routes, "chunk_job", blocking_chunk)

    job = JobSubmit(kind=WorkerKind.OCR, model_id=MODEL_ID, payload_ref=str(corpus))
    submit = asyncio.create_task(
        h.client.post("/v1/admin/jobs", json=job.model_dump(mode="json"), headers=admin_headers())
    )
    for _ in range(500):  # wait until the split is actually parked on the thread
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set(), "submit never reached the chunker"

    ping = await asyncio.wait_for(
        h.client.get("/v1/admin/agents", headers=admin_headers()), timeout=2
    )
    assert ping.status_code == 200  # served while the submit is still splitting

    release.set()
    resp = await asyncio.wait_for(submit, timeout=5)
    assert resp.status_code == 201, resp.text
