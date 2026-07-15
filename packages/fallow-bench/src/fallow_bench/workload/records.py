"""Immutable JSONL record types — the experiment's raw datasets.

Each driver appends one of these per event to its own ``*.jsonl`` file in the
run directory. They reuse :class:`fallow_protocol.FallowModel` so they are
frozen and reject unknown fields, and ``model_dump_json`` renders datetimes as
ISO-8601 for the downstream analysis module.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from fallow_protocol import FallowModel


class RequestStatus(StrEnum):
    """Terminal disposition of one interactive request."""

    OK = "ok"  # streamed a response (any 2xx that produced bytes)
    ERROR = "error"  # connection error or a >=400 status from the gateway
    TIMEOUT = "timeout"  # per-request timeout elapsed before completion


class RequestRecord(FallowModel):
    """One interactive request's timing and outcome."""

    req_id: int
    prompt_idx: int
    t_scheduled: datetime  # run_start + arrival offset (the open-loop target)
    t_submit: datetime  # when the request was actually fired
    t_first_token: datetime | None  # first SSE frame; None if none arrived
    t_done: datetime
    status: RequestStatus
    http_status: int | None
    tokens_out: int  # count of SSE data frames (excluding [DONE])


class JobRecord(FallowModel):
    """One batch-job observation: the submit response or a poll response."""

    event: str  # "submit" | "poll"
    t: datetime
    job_id: str
    state: str
    total_units: int
    done_units: int
    dead_units: int


class PowerSample(FallowModel):
    """One (agent, GPU) power/state observation — the software energy trace.

    Agents with no GPU emit a single row with ``gpu_index`` and the GPU fields
    set to ``None``.
    """

    t: datetime
    agent_id: str
    state: str
    gpu_index: int | None
    power_w: float | None
    util_percent: float | None
    vram_free_mb: int | None
