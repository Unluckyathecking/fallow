"""Module A6 — batch workers.

Runs leased batch work units (embedding, transcription) against LOCAL inference
replicas and turns each into a :class:`fallow_protocol.messages.WorkResult`.
Depends on ``fallow_protocol`` only: leases in, results out. Workers dial local
replica HTTP endpoints; they never import the supervisor or heartbeat.

Public API:

- :class:`Worker` / :class:`WorkOutput` — the worker seam and its output.
- :class:`EmbedWorker`, :class:`TranscribeWorker` — concrete workers.
- :class:`WorkerRegistry` — kind→factory map for the assembly.
- :class:`WorkUnitRunner` — drives one lease to a ``WorkResult``.
- :class:`EmbedConfig`, :class:`TranscribeConfig` — frozen tuning.
- Typed errors: :class:`WorkerError` and its subclasses.
"""

from fallow_agent.workers.config import EmbedConfig, TranscribeConfig
from fallow_agent.workers.embed import EmbedWorker
from fallow_agent.workers.errors import (
    DeferredUploadError,
    WorkerBackendError,
    WorkerError,
    WorkerInputError,
    WorkerNotRegisteredError,
    WorkerUnavailableError,
)
from fallow_agent.workers.registry import WorkerRegistry
from fallow_agent.workers.runner import (
    DeferredWorkResult,
    FetchInput,
    Monotonic,
    UploadResult,
    WorkUnitRunner,
)
from fallow_agent.workers.transcribe import (
    TranscribeWorker,
    WhisperLoader,
    default_whisper_loader,
)
from fallow_agent.workers.types import (
    EndpointResolver,
    LocalEndpoint,
    TranscribeFn,
    TranscriptSegment,
    Worker,
    WorkerFactory,
    WorkOutput,
)

__all__ = [
    "DeferredUploadError",
    "DeferredWorkResult",
    "EmbedConfig",
    "EmbedWorker",
    "EndpointResolver",
    "FetchInput",
    "LocalEndpoint",
    "Monotonic",
    "TranscribeConfig",
    "TranscribeFn",
    "TranscribeWorker",
    "TranscriptSegment",
    "UploadResult",
    "WhisperLoader",
    "WorkOutput",
    "WorkUnitRunner",
    "Worker",
    "WorkerBackendError",
    "WorkerError",
    "WorkerFactory",
    "WorkerInputError",
    "WorkerNotRegisteredError",
    "WorkerRegistry",
    "WorkerUnavailableError",
    "default_whisper_loader",
]
