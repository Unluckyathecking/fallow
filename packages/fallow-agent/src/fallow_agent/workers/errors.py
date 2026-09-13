"""Typed errors raised by the batch-worker module.

Every failure here is a *content* or *availability* failure, not a bug in the
agent: the runner turns any of these (and any other exception) into a FAILED
:class:`fallow_protocol.messages.WorkResult` so a single poisoned unit can never
take the agent down.
"""

from pathlib import Path

from fallow_protocol.capabilities import WorkerKind


class WorkerError(Exception):
    """Base class for all batch-worker failures."""


class WorkerUnavailableError(WorkerError):
    """A worker cannot be *constructed* on this machine.

    Raised at construction time (never at run time) when an optional dependency
    or model artifact is missing — e.g. the ``[whisper]`` extra is not
    installed. Raising early lets the assembly leave that ``WorkerKind`` out of
    the runner so the scheduler avoids leasing units it could never run.
    """


class WorkerNotRegisteredError(WorkerError):
    """A lease named a ``WorkerKind`` for which no worker instance exists."""

    def __init__(self, kind: WorkerKind) -> None:
        super().__init__(f"no worker registered for kind {kind.value!r}")
        self.kind = kind


class WorkerInputError(WorkerError):
    """A work unit's ``input_bytes`` were malformed for the target worker."""


class WorkerBackendError(WorkerError):
    """The local replica returned an error status or an unexpected body."""


class DeferredUploadError(WorkerError):
    """A result is safe locally but must wait for lease-expiry retry."""

    def __init__(self, payload_path: Path, cause: Exception) -> None:
        self.payload_path = payload_path
        self.cause = cause
        super().__init__(f"result payload retained at {payload_path}: {cause}")
