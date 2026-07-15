"""Shared worker seams: the :class:`Worker` protocol and its output type.

These are the abstractions the runner and registry depend on, so concrete
workers (embed, transcribe, and any future kind) are swappable and unit-testable
with plain fakes.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fallow_protocol.messages import WorkMetrics, WorkUnitLease


@dataclass(frozen=True)
class WorkOutput:
    """A worker's result before the runner stamps it with wall-clock duration.

    ``metrics.duration_s`` is a placeholder (``0.0``) here: only the runner owns
    the monotonic clock, so it overwrites the duration when building the final
    :class:`fallow_protocol.messages.WorkResult`.
    """

    payload: bytes
    metrics: WorkMetrics


@dataclass(frozen=True)
class LocalEndpoint:
    """Host/port of a LOCAL inference replica the worker dials over HTTP."""

    host: str
    port: int


class Worker(Protocol):
    """Runs one leased work unit against a local replica and returns bytes."""

    async def run(self, lease: WorkUnitLease, input_bytes: bytes) -> WorkOutput: ...


# Resolves the local replica serving a given ``model_id`` (wave-3 assembly wires
# this to the supervisor's live replica table).
EndpointResolver = Callable[[str], LocalEndpoint]

# Builds a fresh worker; may raise ``WorkerUnavailableError`` (optional deps).
WorkerFactory = Callable[[], Worker]

# One recognised transcription segment: (start_s, end_s, text).
TranscriptSegment = tuple[float, float, str]

# The transcribe seam: turn an audio file into ordered segments. Injected so
# tests never import ``faster_whisper``.
TranscribeFn = Callable[[Path], Sequence[TranscriptSegment]]
