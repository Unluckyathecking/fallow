"""Churn injector (module B2).

Seeded, replay-deterministic fleet-churn generation and an async injector that
replays it against bench-mode agents. See ``docs/adr/020-bench-churn.md``.

Public API:

* :class:`ChurnSection`, :class:`ChurnEvent`, :class:`ChurnRecord`,
  :class:`ChurnKind`, :class:`AgentTarget`, :class:`ChurnModel`,
  :class:`VerifyConfig`, :class:`RunResult` — frozen data models.
* :func:`build_schedule` / :func:`resolve_schedule` — seeded generator.
* :func:`load_churn_section` — YAML loader (standalone or B1-embedded).
* :class:`ChurnInjector` — async replay engine.
* :class:`ChurnLog` — ``churn.jsonl`` sink.
* :func:`measure_flip` — input→yield flip-latency probe.
* :func:`run_shell` — subprocess ``Runner`` (``__main__`` wiring only).
"""

from fallow_bench.churn.config import load_churn_section, parse_churn_section
from fallow_bench.churn.injector import ChurnInjector
from fallow_bench.churn.models import (
    AgentTarget,
    ChurnEvent,
    ChurnKind,
    ChurnModel,
    ChurnRecord,
    ChurnSection,
    RunResult,
    VerifyConfig,
)
from fallow_bench.churn.ports import Clock, RecordSink, Runner, Sleeper
from fallow_bench.churn.runner import run_shell
from fallow_bench.churn.schedule import build_schedule, resolve_schedule
from fallow_bench.churn.verify import measure_flip, state_url
from fallow_bench.churn.writer import ChurnLog

__all__ = [
    "AgentTarget",
    "ChurnEvent",
    "ChurnInjector",
    "ChurnKind",
    "ChurnLog",
    "ChurnModel",
    "ChurnRecord",
    "ChurnSection",
    "Clock",
    "RecordSink",
    "RunResult",
    "Runner",
    "Sleeper",
    "VerifyConfig",
    "build_schedule",
    "load_churn_section",
    "measure_flip",
    "parse_churn_section",
    "resolve_schedule",
    "run_shell",
    "state_url",
]
