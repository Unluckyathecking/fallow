"""Client-side workload generator for the 3-arm scheduling experiment (B1).

Drives one experiment arm: an open-loop interactive load against the
OpenAI-compatible gateway, one batch job submitted and polled via the admin API,
and a 1 Hz agent power/state sampler. The arrival schedule is precomputed from
the config seed so every arm sees identical load; all clocks and HTTP clients
are injected, so a run is replay-deterministic (see ADR 019).
"""

from fallow_bench.workload.clocks import Clocks
from fallow_bench.workload.config import (
    BatchConfig,
    ExperimentConfig,
    InteractiveConfig,
    SamplingConfig,
    load_config,
)
from fallow_bench.workload.records import (
    JobRecord,
    PowerSample,
    RequestRecord,
    RequestStatus,
)
from fallow_bench.workload.runner import WorkloadRunner
from fallow_bench.workload.schedule import Arrival, build_schedule

__all__ = [
    "Arrival",
    "BatchConfig",
    "Clocks",
    "ExperimentConfig",
    "InteractiveConfig",
    "JobRecord",
    "PowerSample",
    "RequestRecord",
    "RequestStatus",
    "SamplingConfig",
    "WorkloadRunner",
    "build_schedule",
    "load_config",
]
