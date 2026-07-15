"""Constants for the preemption state machine.

No magic numbers live in the control logic; they are named here so the hot
path reads as intent, not arithmetic.
"""

from typing import Final

# Milliseconds per second — used to convert poll periods and yield latencies.
MS_PER_S: Final[float] = 1000.0

# Name of the dedicated poll thread (dedicated so preemption never contends
# with the asyncio event loop that runs everything else on the agent).
POLL_THREAD_NAME: Final[str] = "fallow-preempt-poll"

# Detail key carrying the measured yield latency on a USER_RETURNED event.
YIELD_MS_KEY: Final[str] = "yield_ms"
