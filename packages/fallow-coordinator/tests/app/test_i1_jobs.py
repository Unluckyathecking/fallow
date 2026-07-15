"""End-to-end batch flow: submit → chunk → lease → input fetch → result → DONE."""

from __future__ import annotations

import json
from pathlib import Path

from app_helpers import (
    MODEL_ID,
    Harness,
    admin_headers,
    bearer,
    enrolled_idle_agent,
    make_replica,
    make_success_result,
)

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

        # Complete the unit successfully.
        result = make_success_result(lease.work_unit_id)
        done = await h.client.post(
            f"/v1/agents/{agent_id}/work_units/{lease.work_unit_id}/result",
            json=result.model_dump(mode="json"),
            headers=bearer(token),
        )
        assert done.status_code == 200

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
        result = make_success_result(lease.work_unit_id)
        await h.client.post(
            f"/v1/agents/{agent_id}/work_units/{lease.work_unit_id}/result",
            json=result.model_dump(mode="json"),
            headers=bearer(token),
        )

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
