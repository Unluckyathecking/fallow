"""Drive the built Go agent (``cmd/agentctl``) as the agent uplink in parity tests.

Each method shells out to one ``agentctl`` subcommand over ``asyncio`` — never a
blocking ``subprocess.run`` — because the coordinator under test runs uvicorn on
the *same* event loop, so a synchronous call would deadlock. The API mirrors the
Python ``CoordinatorClient`` surface the integration helpers use (``register`` /
``heartbeat`` / ``poll_work`` / ``upload_result`` / ``complete_unit``) so a scenario
can swap the Python agent for the Go one and reuse the same assertions.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fallow_protocol.messages import AgentState
from fallow_protocol.models import ReplicaStatus


class GoAgentError(RuntimeError):
    """An ``agentctl`` invocation exited non-zero; the message is its stderr."""


def _replica_spec(replica: ReplicaStatus) -> str:
    return f"{replica.model_id}:{replica.port}:{replica.state.value}"


class GoAgent:
    """A single Go agent process family bound to one coordinator base URL."""

    def __init__(self, binary: Path, base_url: str) -> None:
        self._binary = str(binary)
        self._base_url = base_url
        self.agent_id: str | None = None
        self.device_token: str | None = None

    async def register(
        self, token: str, *, hostname: str = "pc1", state_path: Path | None = None
    ) -> None:
        args = ["register", "-url", self._base_url, "-token", token, "-hostname", hostname]
        if state_path is not None:
            args += ["-state", str(state_path)]
        data = await self._run(*args)
        self.agent_id = str(data["agent_id"])
        self.device_token = str(data["device_token"])

    async def heartbeat(
        self,
        *,
        state: AgentState = AgentState.IDLE,
        replicas: tuple[ReplicaStatus, ...] = (),
        seq: int = 1,
    ) -> tuple[str, ...]:
        args = [*self._identified("heartbeat"), "-state-name", state.value, "-seq", str(seq)]
        for replica in replicas:
            args += ["-replica", _replica_spec(replica)]
        data = await self._run(*args)
        return tuple(data.get("desired_models") or ())

    async def poll_work(self, timeout: float = 0.0) -> dict[str, Any] | None:
        args = [*self._identified("poll"), "-timeout", str(timeout)]
        data = await self._run(*args)
        lease = data.get("lease")
        return lease if lease is None else dict(lease)

    async def upload_result(self, work_unit_id: str, *, attempt: int, payload: bytes) -> str:
        args = [
            *self._identified("upload"),
            "-unit",
            work_unit_id,
            "-attempt",
            str(attempt),
            "-payload",
            payload.decode(),
        ]
        data = await self._run(*args)
        return str(data["result_ref"])

    async def complete_unit(
        self, work_unit_id: str, *, attempt: int, result_ref: str | None = None
    ) -> None:
        args = [*self._identified("complete"), "-unit", work_unit_id, "-attempt", str(attempt)]
        if result_ref is not None:
            args += ["-result-ref", result_ref]
        await self._run(*args)

    # ── internals ────────────────────────────────────────────────────────────

    def _identified(self, subcommand: str) -> list[str]:
        if self.agent_id is None or self.device_token is None:
            raise GoAgentError(f"{subcommand} requires register() first")
        return [
            subcommand,
            "-url",
            self._base_url,
            "-agent-id",
            self.agent_id,
            "-token",
            self.device_token,
        ]

    async def _run(self, *args: str) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            self._binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode().strip() or f"agentctl {args[0]} exited {proc.returncode}"
            raise GoAgentError(detail)
        text = stdout.decode().strip()
        return dict(json.loads(text)) if text else {}
