"""Seeded, replay-deterministic churn-schedule generator.

``build_schedule`` turns a :class:`ChurnSection` into a sorted tuple of
:class:`ChurnEvent`. All randomness flows from one seeded ``numpy`` generator, so
the same seed yields a byte-identical schedule. ``resolve_schedule`` prefers an
explicit scripted list when the section supplies one.
"""

from __future__ import annotations

import numpy as np

from fallow_bench.churn import constants as k
from fallow_bench.churn.models import ChurnEvent, ChurnKind, ChurnModel, ChurnSection


def resolve_schedule(section: ChurnSection) -> tuple[ChurnEvent, ...]:
    """Return the scripted schedule verbatim, else generate one from the seed."""
    if section.scripted is not None:
        return tuple(sorted(section.scripted, key=_sort_key))
    return build_schedule(section)


def build_schedule(section: ChurnSection) -> tuple[ChurnEvent, ...]:
    """Generate a deterministic schedule for ``section`` from its seed."""
    rng = np.random.default_rng(section.seed)
    events: list[ChurnEvent] = []
    for agent in section.agents:
        events.extend(_agent_returns(agent.name, section.duration_s, section.model, rng))
    events.extend(_rare_events(section, rng))
    events.sort(key=_sort_key)
    return tuple(events)


def _sort_key(event: ChurnEvent) -> tuple[float, str, str]:
    return (event.t_offset_s, event.agent_name, event.kind.value)


def _agent_returns(
    name: str, duration_s: float, model: ChurnModel, rng: np.random.Generator
) -> list[ChurnEvent]:
    """Emit user-return taps for one agent's idle/active renewal process."""
    out: list[ChurnEvent] = []
    clock = 0.0
    session = 0
    while clock < duration_s:
        clock += float(rng.lognormal(model.idle_mu, model.idle_sigma))
        if clock >= duration_s:
            break
        end = min(clock + float(rng.lognormal(model.active_mu, model.active_sigma)), duration_s)
        out.extend(_session_taps(name, clock, end, model.tap_interval_s, session))
        clock = end
        session += 1
    return out


def _session_taps(
    name: str, start: float, end: float, interval_s: float, session: int
) -> list[ChurnEvent]:
    """Taps every ``interval_s`` across one active session keep the machine active."""
    out: list[ChurnEvent] = []
    tap = start
    while tap < end:
        out.append(
            ChurnEvent(
                t_offset_s=round(tap, k.SCHEDULE_TIME_DP),
                agent_name=name,
                kind=ChurnKind.USER_RETURN,
                params={"session": float(session)},
            )
        )
        tap += interval_s
    return out


def _rare_events(section: ChurnSection, rng: np.random.Generator) -> list[ChurnEvent]:
    """Low-rate Poisson kill/net-drop events; empty when both rates are zero."""
    names = [agent.name for agent in section.agents]
    if not names:
        return []
    out: list[ChurnEvent] = []
    rates = (
        (ChurnKind.AGENT_KILL, section.model.kill_rate_per_s),
        (ChurnKind.NET_DROP, section.model.net_drop_rate_per_s),
    )
    for kind, rate in rates:
        if rate <= 0.0:
            continue
        clock = float(rng.exponential(1.0 / rate))
        while clock < section.duration_s:
            name = names[int(rng.integers(0, len(names)))]
            out.append(
                ChurnEvent(t_offset_s=round(clock, k.SCHEDULE_TIME_DP), agent_name=name, kind=kind)
            )
            clock += float(rng.exponential(1.0 / rate))
    return out
