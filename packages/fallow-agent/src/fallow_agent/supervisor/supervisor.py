"""ChildProcessSupervisor: owns fallow-launched inference child processes.

See the module README for the public contract, lifecycle diagram, and the
lock-ordering rule. In short: the single ``_lock`` guards the state dicts and
the cached status tuple only. Blocking work — spawning, psutil suspend/resume,
process wait/kill, /health probes, thread joins — always happens OUTSIDE the
lock, so ``suspend_all``/``resume_all`` stay hot-path fast.
"""

import contextlib
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import psutil

from fallow_agent.supervisor.child import SpawnFn, _Child, default_spawn
from fallow_agent.supervisor.commands import CommandFactory
from fallow_agent.supervisor.config import SupervisorConfig
from fallow_agent.supervisor.health import HealthCheck, http_health_check
from fallow_protocol.interfaces import ProcessSupervisor
from fallow_protocol.models import ModelManifest, ReplicaState, ReplicaStatus

logger = logging.getLogger(__name__)

MonotonicFn = Callable[[], float]


class ChildProcessSupervisor(ProcessSupervisor):
    """Concrete `ProcessSupervisor` over local OS processes.

    Clocks and I/O are injected (``monotonic``, ``spawn``, ``health_check``)
    so lifecycle behaviour is deterministic in tests. One replica per
    ``model_id``; port allocation is the caller's responsibility.
    """

    def __init__(
        self,
        config: SupervisorConfig,
        command_factory: CommandFactory,
        *,
        health_check: HealthCheck = http_health_check,
        monotonic: MonotonicFn = time.monotonic,
        spawn: SpawnFn = default_spawn,
    ) -> None:
        self._config = config
        self._command_factory = command_factory
        self._health_check = health_check
        self._monotonic = monotonic
        self._spawn = spawn
        self._lock = threading.Lock()
        self._children: dict[str, _Child] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._states: dict[str, ReplicaState] = {}
        self._ports: dict[str, int] = {}
        self._gpu: dict[str, bool] = {}
        self._pre_suspend: dict[str, ReplicaState] = {}
        self._cached: tuple[ReplicaStatus, ...] = ()

    # ── ProcessSupervisor API ────────────────────────────────────────────

    def start_replica(self, manifest: ModelManifest, model_path: Path, port: int) -> None:
        with self._lock:
            if manifest.model_id in self._children:
                logger.warning("replica %s already running; ignoring start", manifest.model_id)
                return
        cmd = self._command_factory(manifest, model_path, port)
        popen = self._spawn(cmd)
        child = self._register_child(manifest.model_id, port, popen, gpu=manifest.min_vram_mb > 0)
        thread = threading.Thread(
            target=self._health_loop,
            args=(child,),
            name=f"fallow-health-{manifest.model_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[child.model_id] = thread
        thread.start()

    def stop_replica(self, model_id: str) -> None:
        with self._lock:
            child = self._children.pop(model_id, None)
            thread = self._threads.pop(model_id, None)
            self._pre_suspend.pop(model_id, None)
            self._set_state_locked(model_id, ReplicaState.STOPPED)
        if child is None:
            return
        child.shutdown.set()
        self._graceful_terminate(child)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._config.stop_grace_s + 1.0)

    def stop_all(self) -> None:
        with self._lock:
            model_ids = tuple(self._children.keys())
        for model_id in model_ids:
            self.stop_replica(model_id)

    def suspend_all(self) -> None:
        self._apply_signal(suspend=True)

    def resume_all(self) -> None:
        self._apply_signal(suspend=False)

    def statuses(self) -> tuple[ReplicaStatus, ...]:
        with self._lock:
            return self._cached

    # ── Registration / spawn helpers ─────────────────────────────────────

    def _register_child(
        self, model_id: str, port: int, popen: "subprocess.Popen[bytes]", *, gpu: bool
    ) -> _Child:
        try:
            proc = psutil.Process(popen.pid)
        except psutil.NoSuchProcess:
            proc = None
        child = _Child(
            model_id=model_id,
            port=port,
            popen=popen,
            proc=proc,
            started_monotonic=self._monotonic(),
            shutdown=threading.Event(),
        )
        with self._lock:
            self._children[model_id] = child
            self._ports[model_id] = port
            self._gpu[model_id] = gpu
            self._set_state_locked(model_id, ReplicaState.LOADING)
        return child

    # ── Health / crash-detection thread ──────────────────────────────────

    def _health_loop(self, child: _Child) -> None:
        if self._await_ready(child):
            self._watch_until_exit(child)

    def _await_ready(self, child: _Child) -> bool:
        deadline = child.started_monotonic + self._config.startup_timeout_s
        while not child.shutdown.is_set():
            if child.popen.poll() is not None:
                self._mark_crashed(child, "process exited during startup")
                return False
            if self._probe(child):
                self._mark_ready(child.model_id)
                return True
            if self._monotonic() >= deadline:
                self._on_startup_timeout(child)
                return False
            child.shutdown.wait(self._config.health_poll_interval_s)
        return False

    def _watch_until_exit(self, child: _Child) -> None:
        while not child.shutdown.is_set():
            if child.popen.poll() is not None:
                self._mark_crashed(child, "process exited unexpectedly")
                return
            child.shutdown.wait(self._config.health_poll_interval_s)

    def _probe(self, child: _Child) -> bool:
        return self._health_check(
            self._config.bind_host,
            child.port,
            self._config.health_path,
            self._config.health_timeout_s,
        )

    def _on_startup_timeout(self, child: _Child) -> None:
        logger.error(
            "replica %s failed to become ready within %ss; killing",
            child.model_id,
            self._config.startup_timeout_s,
        )
        self._force_kill(child)
        self._mark_crashed(child, "startup timeout")

    # ── State transitions (all mutate under the lock) ────────────────────

    def _mark_ready(self, model_id: str) -> None:
        with self._lock:
            if self._states.get(model_id) is ReplicaState.LOADING:
                self._set_state_locked(model_id, ReplicaState.READY)
                logger.info("replica %s ready", model_id)

    def _mark_crashed(self, child: _Child, reason: str) -> None:
        with self._lock:
            self._children.pop(child.model_id, None)
            self._pre_suspend.pop(child.model_id, None)
            if child.model_id in self._states:
                self._set_state_locked(child.model_id, ReplicaState.STOPPED)
        self._reap(child)
        logger.error("replica %s stopped: %s", child.model_id, reason)

    def _set_state_locked(self, model_id: str, state: ReplicaState) -> None:
        if model_id not in self._ports and state is ReplicaState.STOPPED:
            return  # never knew this replica; nothing to record
        self._states[model_id] = state
        self._rebuild_cache_locked()

    def _rebuild_cache_locked(self) -> None:
        self._cached = tuple(
            ReplicaStatus(
                model_id=model_id,
                port=self._ports[model_id],
                state=state,
                gpu=self._gpu.get(model_id, False),
            )
            for model_id, state in self._states.items()
        )

    # ── Suspend / resume (hot path) ──────────────────────────────────────

    def _apply_signal(self, *, suspend: bool) -> None:
        with self._lock:
            snapshot = tuple(self._children.values())
        vanished = self._signal_processes(snapshot, suspend=suspend)
        self._commit_signal(snapshot, vanished, suspend=suspend)

    def _signal_processes(self, snapshot: tuple[_Child, ...], *, suspend: bool) -> set[str]:
        vanished: set[str] = set()
        for child in snapshot:
            # Check the Popen we own before touching psutil. On Windows a reaped
            # process can surface as AccessDenied instead of NoSuchProcess, and
            # signalling a stale/reused PID would be unsafe on every platform.
            if child.proc is None or child.popen.poll() is not None:
                vanished.add(child.model_id)
                continue
            try:
                child.proc.suspend() if suspend else child.proc.resume()
            except psutil.NoSuchProcess:
                vanished.add(child.model_id)
        return vanished

    def _commit_signal(
        self, snapshot: tuple[_Child, ...], vanished: set[str], *, suspend: bool
    ) -> None:
        with self._lock:
            for child in snapshot:
                model_id = child.model_id
                if model_id in vanished:
                    child.shutdown.set()  # let its health thread exit promptly
                    self._prune_locked(model_id)
                elif model_id in self._states:
                    self._transition_signal_locked(model_id, suspend=suspend)
            self._rebuild_cache_locked()

    def _transition_signal_locked(self, model_id: str, *, suspend: bool) -> None:
        current = self._states[model_id]
        if suspend:
            if current is not ReplicaState.SUSPENDED:
                self._pre_suspend[model_id] = current
            self._states[model_id] = ReplicaState.SUSPENDED
        else:
            self._states[model_id] = self._pre_suspend.pop(model_id, current)

    def _prune_locked(self, model_id: str) -> None:
        self._children.pop(model_id, None)
        self._threads.pop(model_id, None)
        self._pre_suspend.pop(model_id, None)
        if model_id in self._states:
            self._states[model_id] = ReplicaState.STOPPED

    # ── Process teardown (never under the lock) ──────────────────────────

    def _graceful_terminate(self, child: _Child) -> None:
        with contextlib.suppress(OSError):
            child.popen.terminate()
        if self._wait(child, self._config.stop_grace_s):
            return
        self._force_kill(child)

    def _force_kill(self, child: _Child) -> None:
        with contextlib.suppress(OSError):
            child.popen.kill()
        if not self._wait(child, self._config.stop_grace_s):
            logger.error("replica %s did not exit after kill", child.model_id)

    def _wait(self, child: _Child, timeout_s: float) -> bool:
        try:
            child.popen.wait(timeout=timeout_s)
            return True
        except subprocess.TimeoutExpired:
            return False

    def _reap(self, child: _Child) -> None:
        try:
            child.popen.wait(timeout=self._config.stop_grace_s)
        except subprocess.TimeoutExpired:
            logger.error("replica %s not reaped after exit signal", child.model_id)
