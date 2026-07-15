"""``WorkUnitRunner``: drives one lease end-to-end into a ``WorkResult``.

The runner owns the only clock and the only ``try``: it fetches input, picks the
worker by ``lease.kind``, runs it, uploads the payload, and stamps the measured
duration. Worker, input, and permanent upload errors become FAILED results. A
deferred upload returns an internal signal so the work loop leaves the lease
uncompleted for expiry retry. A worker must never be able to kill the agent.
"""

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from fallow_agent.workers.errors import DeferredUploadError, WorkerNotRegisteredError
from fallow_agent.workers.types import Worker
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import (
    WorkMetrics,
    WorkResult,
    WorkResultStatus,
    WorkUnitLease,
)

# Fetch this unit's input bytes from the coordinator (agent-initiated).
FetchInput = Callable[[WorkUnitLease], Awaitable[bytes]]

# Upload the result payload and return the coordinator-side result_ref.
UploadResult = Callable[[WorkUnitLease, bytes], Awaitable[str]]

# Injected monotonic clock (seconds); tests pass a deterministic fake.
Monotonic = Callable[[], float]


@dataclass(frozen=True)
class DeferredWorkResult:
    """A successful computation whose payload awaits a later lease attempt."""

    work_unit_id: str
    payload_path: Path


class WorkUnitRunner:
    """Orchestrate a unit into a completed or deferred result."""

    def __init__(
        self,
        *,
        workers: Mapping[WorkerKind, Worker],
        fetch_input: FetchInput,
        upload: UploadResult,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self._workers = dict(workers)
        self._fetch_input = fetch_input
        self._upload = upload
        self._monotonic = monotonic

    async def run_lease(self, lease: WorkUnitLease) -> WorkResult | DeferredWorkResult:
        started = self._monotonic()
        try:
            payload, metrics = await self._execute(lease)
            result_ref = await self._upload(lease, payload)
        except DeferredUploadError as exc:
            return DeferredWorkResult(
                work_unit_id=lease.work_unit_id, payload_path=exc.payload_path
            )
        except Exception as exc:  # any non-deferred failure becomes a FAILED result
            return self._failed(lease, exc, started)
        duration = max(self._monotonic() - started, 0.0)
        return WorkResult(
            work_unit_id=lease.work_unit_id,
            status=WorkResultStatus.SUCCEEDED,
            result_ref=result_ref,
            metrics=metrics.model_copy(update={"duration_s": duration}),
        )

    async def _execute(self, lease: WorkUnitLease) -> tuple[bytes, WorkMetrics]:
        worker = self._select(lease.kind)
        input_bytes = await self._fetch_input(lease)
        output = await worker.run(lease, input_bytes)
        return output.payload, output.metrics

    def _select(self, kind: WorkerKind) -> Worker:
        try:
            return self._workers[kind]
        except KeyError as exc:
            raise WorkerNotRegisteredError(kind) from exc

    def _failed(self, lease: WorkUnitLease, exc: Exception, started: float) -> WorkResult:
        duration = max(self._monotonic() - started, 0.0)
        return WorkResult(
            work_unit_id=lease.work_unit_id,
            status=WorkResultStatus.FAILED,
            error=_describe(exc),
            metrics=WorkMetrics(duration_s=duration),
        )


def _describe(exc: Exception) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
