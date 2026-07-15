"""Unit tests for module A3 — ChildProcessSupervisor.

Tests use real tiny child processes (a Python sleeper) driven through an
injected CommandFactory, plus a fake health check so no HTTP happens. SIGSTOP
via psutil works on macOS/Linux dev + CI, so suspend/resume assertions inspect
real process status.
"""

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from fallow_agent.supervisor import (
    ChildProcessSupervisor,
    LlamaServerCommandFactory,
    SupervisorConfig,
    llama_server_command,
)
from fallow_agent.supervisor.config import FORBIDDEN_BIND_HOST
from fallow_protocol.models import ModelManifest, ReplicaState, WorkerKind

SLEEP_SECONDS = 60
SHA = "a" * 64
FAST_POLL_S = 0.01
DEADLINE_S = 0.5


def _manifest(model_id: str = "tiny", min_vram_mb: int = 0) -> ModelManifest:
    return ModelManifest(
        model_id=model_id,
        family="tiny",
        quant="Q4_K_M",
        worker_kind=WorkerKind.CHAT,
        file_name="tiny.gguf",
        sha256=SHA,
        size_bytes=1,
        min_vram_mb=min_vram_mb,
        default_args=("--extra", "1"),
    )


def _sleeper_command(_manifest: ModelManifest, _model_path: Path, _port: int) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({SLEEP_SECONDS})"]


def _fast_config(**overrides: object) -> SupervisorConfig:
    base: dict[str, object] = {
        "llama_binary": Path("/usr/bin/true"),
        "startup_timeout_s": DEADLINE_S,
        "health_poll_interval_s": FAST_POLL_S,
        "stop_grace_s": DEADLINE_S,
    }
    base.update(overrides)
    return SupervisorConfig(**base)  # type: ignore[arg-type]


def _wait_for(predicate: Callable[[], bool], timeout: float = DEADLINE_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(FAST_POLL_S)
    return predicate()


def _state_of(sup: ChildProcessSupervisor, model_id: str) -> ReplicaState | None:
    for status in sup.statuses():
        if status.model_id == model_id:
            return status.state
    return None


class _AlwaysHealthy:
    def __call__(self, host: str, port: int, path: str, timeout_s: float) -> bool:
        return True


class _NeverHealthy:
    def __call__(self, host: str, port: int, path: str, timeout_s: float) -> bool:
        return False


@pytest.fixture
def supervisor() -> ChildProcessSupervisor:
    return ChildProcessSupervisor(
        _fast_config(),
        _sleeper_command,
        health_check=_AlwaysHealthy(),
    )


def _cleanup(sup: ChildProcessSupervisor) -> None:
    sup.stop_all()


# ── Command construction ─────────────────────────────────────────────────────


def test_llama_command_cpu_has_no_gpu_flags() -> None:
    config = SupervisorConfig(llama_binary=Path("/opt/llama-server"), bind_host="100.64.0.1")
    factory = llama_server_command(config)
    cmd = factory(_manifest(min_vram_mb=0), Path("/models/tiny.gguf"), 8080)
    assert cmd[0] == str(config.llama_binary)
    assert "--host" in cmd and cmd[cmd.index("--host") + 1] == "100.64.0.1"
    assert cmd[cmd.index("--parallel") + 1] == "2"
    assert cmd[cmd.index("-c") + 1] == "8192"
    assert cmd[-2:] == ["--extra", "1"]  # default_args appended, no gpu flags
    assert "-ngl" not in cmd and "--flash-attn" not in cmd


def test_llama_command_gpu_appends_offload_flags() -> None:
    factory = LlamaServerCommandFactory(SupervisorConfig(llama_binary=Path("llama-server")))
    cmd = factory(_manifest(min_vram_mb=4096), Path("/models/tiny.gguf"), 9000)
    assert cmd[-3:] == ["-ngl", "999", "--flash-attn"]
    assert cmd.index("--extra") < cmd.index("-ngl")  # default_args precede gpu flags


def test_config_rejects_wildcard_bind_host() -> None:
    with pytest.raises(ValueError, match=r"0\.0\.0\.0"):
        SupervisorConfig(llama_binary=Path("x"), bind_host=FORBIDDEN_BIND_HOST)


# ── Lifecycle ────────────────────────────────────────────────────────────────


def test_start_reports_loading_then_ready(supervisor: ChildProcessSupervisor) -> None:
    try:
        supervisor.start_replica(_manifest(), Path("/models/tiny.gguf"), 8080)
        assert _wait_for(lambda: _state_of(supervisor, "tiny") is ReplicaState.READY)
        (status,) = supervisor.statuses()
        assert status.port == 8080
        assert status.inflight == 0
    finally:
        _cleanup(supervisor)


def test_duplicate_start_is_ignored(supervisor: ChildProcessSupervisor) -> None:
    try:
        supervisor.start_replica(_manifest(), Path("/m.gguf"), 8080)
        assert _wait_for(lambda: _state_of(supervisor, "tiny") is ReplicaState.READY)
        supervisor.start_replica(_manifest(), Path("/m.gguf"), 9999)  # ignored
        assert len(supervisor.statuses()) == 1
        assert supervisor.statuses()[0].port == 8080
    finally:
        _cleanup(supervisor)


def test_stop_replica_kills_within_grace(supervisor: ChildProcessSupervisor) -> None:
    supervisor.start_replica(_manifest(), Path("/m.gguf"), 8080)
    assert _wait_for(lambda: _state_of(supervisor, "tiny") is ReplicaState.READY)
    pid = supervisor._children["tiny"].popen.pid
    started = time.monotonic()
    supervisor.stop_replica("tiny")
    elapsed = time.monotonic() - started
    assert elapsed < DEADLINE_S + 1.0
    assert _state_of(supervisor, "tiny") is ReplicaState.STOPPED
    assert not psutil.pid_exists(pid) or _wait_for(lambda: not psutil.pid_exists(pid))


def test_suspend_and_resume_change_process_status(supervisor: ChildProcessSupervisor) -> None:
    try:
        supervisor.start_replica(_manifest(), Path("/m.gguf"), 8080)
        assert _wait_for(lambda: _state_of(supervisor, "tiny") is ReplicaState.READY)
        proc = supervisor._children["tiny"].proc

        supervisor.suspend_all()
        assert _state_of(supervisor, "tiny") is ReplicaState.SUSPENDED
        assert _wait_for(
            lambda: proc.status() in (psutil.STATUS_STOPPED, psutil.STATUS_TRACING_STOP)
        )

        supervisor.resume_all()
        assert _state_of(supervisor, "tiny") is ReplicaState.READY
        assert _wait_for(lambda: proc.status() != psutil.STATUS_STOPPED)
    finally:
        _cleanup(supervisor)


def test_suspend_all_is_fast(supervisor: ChildProcessSupervisor) -> None:
    try:
        for i in range(3):
            supervisor.start_replica(_manifest(f"m{i}"), Path("/m.gguf"), 8080 + i)
        assert _wait_for(lambda: all(s.state is ReplicaState.READY for s in supervisor.statuses()))
        started = time.perf_counter()
        supervisor.suspend_all()
        assert (time.perf_counter() - started) < 0.05  # generous vs the <10ms target
    finally:
        _cleanup(supervisor)


def test_suspend_all_with_vanished_process_does_not_raise() -> None:
    # Long poll interval so the crash-detection thread does not reap first;
    # we reap the child ourselves, guaranteeing psutil raises NoSuchProcess
    # from inside suspend_all's snapshot.
    sup = ChildProcessSupervisor(
        _fast_config(health_poll_interval_s=30.0),
        _sleeper_command,
        health_check=_AlwaysHealthy(),
    )
    try:
        sup.start_replica(_manifest(), Path("/m.gguf"), 8080)
        assert _wait_for(lambda: _state_of(sup, "tiny") is ReplicaState.READY)
        child = sup._children["tiny"]
        child.proc.kill()
        child.popen.wait(timeout=DEADLINE_S)  # fully reap: PID now gone
        sup.suspend_all()  # must not raise despite the vanished process
        assert _state_of(sup, "tiny") is ReplicaState.STOPPED
    finally:
        _cleanup(sup)


def test_crashed_child_becomes_stopped(supervisor: ChildProcessSupervisor) -> None:
    try:
        supervisor.start_replica(_manifest(), Path("/m.gguf"), 8080)
        assert _wait_for(lambda: _state_of(supervisor, "tiny") is ReplicaState.READY)
        # External kill; the health thread's reap loop must notice.
        psutil.Process(supervisor._children["tiny"].popen.pid).kill()
        assert _wait_for(lambda: _state_of(supervisor, "tiny") is ReplicaState.STOPPED)
    finally:
        _cleanup(supervisor)


def test_startup_timeout_marks_stopped() -> None:
    sup = ChildProcessSupervisor(
        _fast_config(startup_timeout_s=0.05),
        _sleeper_command,
        health_check=_NeverHealthy(),
    )
    try:
        sup.start_replica(_manifest(), Path("/m.gguf"), 8080)
        assert _wait_for(lambda: _state_of(sup, "tiny") is ReplicaState.STOPPED)
    finally:
        _cleanup(sup)


def test_stop_all_stops_every_replica(supervisor: ChildProcessSupervisor) -> None:
    for i in range(2):
        supervisor.start_replica(_manifest(f"m{i}"), Path("/m.gguf"), 8080 + i)
    assert _wait_for(lambda: all(s.state is ReplicaState.READY for s in supervisor.statuses()))
    supervisor.stop_all()
    assert all(s.state is ReplicaState.STOPPED for s in supervisor.statuses())


def test_no_health_threads_left_after_stop(supervisor: ChildProcessSupervisor) -> None:
    supervisor.start_replica(_manifest(), Path("/m.gguf"), 8080)
    assert _wait_for(lambda: _state_of(supervisor, "tiny") is ReplicaState.READY)
    supervisor.stop_replica("tiny")  # joins the health thread before returning
    assert _wait_for(lambda: all(t.name != "fallow-health-tiny" for t in threading.enumerate()))
