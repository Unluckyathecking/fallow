"""Shared fixtures/fakes for the batch-worker tests.

No real network, no llama-server, no faster-whisper, no GPU: everything is a
plain in-memory fake or an httpx.MockTransport.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from fallow_agent.workers.types import WorkOutput
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import WorkMetrics, WorkUnitLease

FAR_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


def make_lease(
    *,
    kind: WorkerKind = WorkerKind.EMBED,
    model_id: str = "bge-small",
    work_unit_id: str = "unit-1",
) -> WorkUnitLease:
    return WorkUnitLease(
        work_unit_id=work_unit_id,
        job_id="job-1",
        kind=kind,
        model_id=model_id,
        input_url="http://coordinator/in/unit-1",
        lease_expires=FAR_FUTURE,
        attempt=1,
    )


class FakeClock:
    """Deterministic monotonic clock returning successive canned readings."""

    def __init__(self, readings: Sequence[float]) -> None:
        self._readings = list(readings)

    def __call__(self) -> float:
        if len(self._readings) > 1:
            return self._readings.pop(0)
        return self._readings[0]


class FakeWorker:
    """A worker that returns a canned output or raises a canned exception."""

    def __init__(
        self,
        *,
        output: WorkOutput | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._output = output or WorkOutput(
            payload=b"payload", metrics=WorkMetrics(duration_s=0.0, items=1)
        )
        self._raises = raises
        self.calls: list[tuple[str, bytes]] = []

    async def run(self, lease: WorkUnitLease, input_bytes: bytes) -> WorkOutput:
        self.calls.append((lease.work_unit_id, input_bytes))
        if self._raises is not None:
            raise self._raises
        return self._output
