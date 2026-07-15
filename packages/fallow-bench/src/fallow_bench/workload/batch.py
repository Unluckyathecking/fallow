"""Batch-job driver: submit one job mid-run and poll it to its own JSONL.

At ``submit_at_s`` the driver POSTs a :class:`JobSubmit` to the admin API,
records the returned :class:`JobStatus`, then polls ``GET /jobs/{id}`` every
``poll_interval_s`` until the job is terminal or the run's duration elapses.
Timing uses the injected clocks so the poll cadence is deterministic in tests.
"""

from __future__ import annotations

from fallow_bench.workload.admin import BenchAdminClient
from fallow_bench.workload.clocks import Clocks
from fallow_bench.workload.records import JobRecord
from fallow_bench.workload.writer import JsonlWriter
from fallow_protocol import JobState, JobStatus, JobSubmit

_TERMINAL = frozenset({JobState.DONE, JobState.CANCELLED})


class BatchDriver:
    """Submits and polls the single batch job for one run."""

    def __init__(
        self,
        *,
        admin: BenchAdminClient,
        job: JobSubmit,
        submit_at_s: float,
        poll_interval_s: float,
        duration_s: float,
        clocks: Clocks,
        writer: JsonlWriter,
    ) -> None:
        self._admin = admin
        self._job = job
        self._submit_at_s = submit_at_s
        self._poll_interval_s = poll_interval_s
        self._duration_s = duration_s
        self._clocks = clocks
        self._writer = writer

    async def run(self) -> None:
        start = self._clocks.monotonic()
        if self._submit_at_s > 0:
            await self._clocks.sleep(self._submit_at_s)
        status = await self._admin.submit_job(self._job)
        self._record("submit", status)
        await self._poll_until_done(start, status)

    async def _poll_until_done(self, start: float, status: JobStatus) -> None:
        while not self._finished(start, status):
            await self._clocks.sleep(self._poll_interval_s)
            status = await self._admin.get_job(status.job_id)
            self._record("poll", status)

    def _finished(self, start: float, status: JobStatus) -> bool:
        if status.state in _TERMINAL:
            return True
        return (self._clocks.monotonic() - start) >= self._duration_s

    def _record(self, event: str, status: JobStatus) -> None:
        self._writer.write(
            JobRecord(
                event=event,
                t=self._clocks.now(),
                job_id=status.job_id,
                state=str(status.state),
                total_units=status.total_units,
                done_units=status.done_units,
                dead_units=status.dead_units,
            )
        )
