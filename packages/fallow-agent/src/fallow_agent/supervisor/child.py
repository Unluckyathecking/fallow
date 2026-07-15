"""Per-replica bookkeeping and the process-spawn seam.

`_Child` is the immutable handle the supervisor keeps for one live replica:
identity plus the OS/psutil handles and a private shutdown Event its health
thread watches. Mutable lifecycle state (LOADING/READY/…) is tracked
separately in the supervisor, guarded by its lock, so this record never
changes after construction.
"""

import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

import psutil

SpawnFn = Callable[[list[str]], "subprocess.Popen[bytes]"]


def default_spawn(cmd: list[str]) -> "subprocess.Popen[bytes]":
    """Spawn a child process with no shell and detached stdio.

    argv is built by a `CommandFactory` (never a shell string). stdout/stderr
    go to DEVNULL: llama-server is chatty and the supervisor tracks liveness
    via the process handle and /health, not via its logs.
    """
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


@dataclass(frozen=True)
class _Child:
    """Immutable handle to one launched replica process."""

    model_id: str
    port: int
    popen: "subprocess.Popen[bytes]"
    proc: psutil.Process | None  # None if the process vanished before tracking
    started_monotonic: float
    shutdown: threading.Event
