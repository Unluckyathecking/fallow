"""Bench hooks (module A7).

Lets the churn injector (module B2) simulate a "user returned" event on an agent
running headless in a benchmark — with no real HID input — via two seams:

- :class:`BenchIdleDetector` — wraps any ``IdleDetector`` and injects a synthetic
  idle reset that the preemption poll thread observes exactly like real input,
  yielding to the (simulated) user. Real input always takes precedence.
- :class:`BenchListener` — a stdlib-only asyncio HTTP surface (no ``fastapi``)
  exposing ``POST /simulate_input`` and ``GET /state`` for the injector.

Enabled only when the operator sets ``[bench] enabled = true`` in agent settings.
See ``docs/adr/018-bench-hooks.md`` and this package's ``README.md``.
"""

from fallow_agent.bench.idle import BenchIdleDetector
from fallow_agent.bench.listener import BenchListener

__all__ = ["BenchIdleDetector", "BenchListener"]
