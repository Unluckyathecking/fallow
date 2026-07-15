"""Shared fakes and helpers for the agent-runtime (module I2) tests.

Everything is in-process and deterministic: fake supervisor/preemptor/modelstore,
an injected clock and sleep, and ``httpx.MockTransport`` for any coordinator
call. No network, no llama-server, no GPU.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fallow_agent.main.settings import AgentSettings, PortRange
from fallow_protocol.capabilities import DeviceCaps, OsFamily
from fallow_protocol.messages import (
    AgentState,
    WorkMetrics,
    WorkResult,
    WorkResultStatus,
    WorkUnitLease,
)
from fallow_protocol.models import ModelManifest, ReplicaState, ReplicaStatus

FIXED_NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
SHA = "a" * 64


def fixed_now() -> datetime:
    return FIXED_NOW


async def instant_sleep(_seconds: float) -> None:
    return None


def sample_caps(_agent_version: str = "0.1.0") -> DeviceCaps:
    return DeviceCaps(
        hostname="box-1",
        os=OsFamily.LINUX,
        os_version="6.1",
        cpu_model="Test CPU",
        cpu_cores=8,
        ram_mb=32000,
        disk_free_mb=100000,
        agent_version="0.1.0",
    )


def make_settings(tmp_path: Path, **overrides: object) -> AgentSettings:
    base: dict[str, object] = {
        "coordinator_url": "http://coordinator.test",
        "bind_host": "127.0.0.1",
        "llama_server_binary": tmp_path / "llama-server",
        "enrollment_token": "enroll-xyz",
        "state_path": tmp_path / "agent-state.json",
        "cache_dir": tmp_path / "models",
        "events_jsonl_path": tmp_path / "events.jsonl",
        "results_dir": tmp_path / "results",
        "port_range": PortRange(start=8100, count=4),
    }
    base.update(overrides)
    return AgentSettings.model_validate(base)


def manifest(model_id: str = "m1", min_vram_mb: int = 0) -> ModelManifest:
    return ModelManifest(
        model_id=model_id,
        family="fam",
        quant="Q4_K_M",
        file_name=f"{model_id}.gguf",
        sha256=SHA,
        size_bytes=1,
        min_vram_mb=min_vram_mb,
    )


def status(model_id: str, state: ReplicaState, port: int = 8100) -> ReplicaStatus:
    return ReplicaStatus(model_id=model_id, port=port, state=state)


def lease(model_id: str = "m1", expires: datetime | None = None) -> WorkUnitLease:
    from fallow_protocol.capabilities import WorkerKind

    return WorkUnitLease(
        work_unit_id="unit-1",
        job_id="job-1",
        kind=WorkerKind.EMBED,
        model_id=model_id,
        input_url="http://coordinator.test/input/unit-1",
        lease_expires=expires or datetime(2026, 7, 15, 12, 5, 0, tzinfo=UTC),
        attempt=1,
    )


def ok_result(unit_id: str = "unit-1") -> WorkResult:
    return WorkResult(
        work_unit_id=unit_id,
        status=WorkResultStatus.SUCCEEDED,
        result_ref="ref-1",
        metrics=WorkMetrics(duration_s=1.0),
    )


class FakePreemptor:
    """Settable preemptor state with a recording ``drain``."""

    def __init__(self, state: AgentState = AgentState.IDLE, log: list[str] | None = None) -> None:
        self._state = state
        self._log = log if log is not None else []

    def set_state(self, state: AgentState) -> None:
        self._state = state

    @property
    def state(self) -> AgentState:
        return self._state

    def drain(self) -> None:
        self._state = AgentState.DRAINING
        self._log.append("drain")


class FakeSupervisor:
    """Records start/stop calls and serves canned statuses."""

    def __init__(
        self, statuses: tuple[ReplicaStatus, ...] = (), log: list[str] | None = None
    ) -> None:
        self._statuses = statuses
        self._log = log if log is not None else []
        self.started: list[tuple[str, int]] = []
        self.stopped: list[str] = []

    def set_statuses(self, statuses: tuple[ReplicaStatus, ...]) -> None:
        self._statuses = statuses

    def start_replica(self, m: ModelManifest, model_path: Path, port: int) -> None:
        self.started.append((m.model_id, port))
        self._log.append(f"start:{m.model_id}:{port}")

    def stop_replica(self, model_id: str) -> None:
        self.stopped.append(model_id)
        self._log.append(f"stop:{model_id}")

    def suspend_all(self) -> None:
        self._log.append("suspend_all")

    def resume_all(self) -> None:
        self._log.append("resume_all")

    def stop_all(self) -> None:
        self._log.append("stop_all")

    def statuses(self) -> tuple[ReplicaStatus, ...]:
        return self._statuses


class FakeModelStore:
    """A ModelStore whose ensure returns a fixed path and records calls."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.ensured: list[str] = []

    def path_if_present(self, m: ModelManifest) -> Path | None:
        return None

    async def ensure(self, m: ModelManifest) -> Path:
        self.ensured.append(m.model_id)
        return self._path
