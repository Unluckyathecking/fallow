"""Typed async client for the coordinator admin API used by the workload.

Only the three routes the bench needs are wrapped: submit a batch job, poll its
status, and list agents (for the power sampler). The :class:`httpx.AsyncClient`
is injected so tests drive it with ``httpx.MockTransport`` — no real network.
See ``docs/admin-api.md`` for the contract.
"""

from __future__ import annotations

import httpx

from fallow_protocol import AgentSnapshot, JobStatus, JobSubmit

_JOBS_PATH = "/v1/admin/jobs"
_AGENTS_PATH = "/v1/admin/agents"


class BenchAdminClient:
    """Thin async wrapper over the admin routes the workload driver calls."""

    def __init__(self, client: httpx.AsyncClient, admin_key: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {admin_key}"}

    async def submit_job(self, job: JobSubmit) -> JobStatus:
        resp = await self._client.post(
            _JOBS_PATH, json=job.model_dump(mode="json"), headers=self._headers
        )
        resp.raise_for_status()
        return JobStatus.model_validate(resp.json())

    async def get_job(self, job_id: str) -> JobStatus:
        resp = await self._client.get(f"{_JOBS_PATH}/{job_id}", headers=self._headers)
        resp.raise_for_status()
        return JobStatus.model_validate(resp.json())

    async def list_agents(self) -> tuple[AgentSnapshot, ...]:
        resp = await self._client.get(_AGENTS_PATH, headers=self._headers)
        resp.raise_for_status()
        return tuple(AgentSnapshot.model_validate(item) for item in resp.json())
