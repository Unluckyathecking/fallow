"""Structural seams the runtime composes against.

The runtime needs two capabilities that live on concrete classes rather than the
``fallow_protocol`` ABCs: ``stop_all`` on the supervisor and ``drain`` on the
preemptor. Typing against these Protocols (not the concrete classes) keeps the
composition testable — a fake supervisor / preemptor satisfies them structurally
— while the real ``ChildProcessSupervisor`` / ``PreemptController`` also do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from fallow_protocol.messages import AgentState
from fallow_protocol.models import ModelManifest, ReplicaStatus


class SupervisorLike(Protocol):
    """A process supervisor the runtime can start, stop, and inspect.

    A structural superset of ``fallow_protocol.interfaces.ProcessSupervisor``
    (it adds ``stop_all``), so the real ``ChildProcessSupervisor`` satisfies it
    and the runtime can still hand it to collaborators that want the nominal
    ABC (via an explicit cast at that one seam).
    """

    def start_replica(self, manifest: ModelManifest, model_path: Path, port: int) -> None: ...

    def stop_replica(self, model_id: str) -> None: ...

    def suspend_all(self) -> None: ...

    def resume_all(self) -> None: ...

    def stop_all(self) -> None: ...

    def statuses(self) -> tuple[ReplicaStatus, ...]: ...


class PreemptorLike(Protocol):
    """A preemptor whose current state is readable and can be drained."""

    @property
    def state(self) -> AgentState: ...

    def drain(self) -> None: ...
