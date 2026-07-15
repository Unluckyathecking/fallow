"""YAML config-loading tests: standalone doc, B1-embedded, scripted, validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fallow_bench.churn import ChurnKind, load_churn_section, parse_churn_section, resolve_schedule

_STANDALONE = textwrap.dedent(
    """
    agents:
      - {name: mac, host: 10.0.0.1}
      - {name: win, host: 10.0.0.2, bench_port: 9500}
    duration_s: 300.0
    seed: 11
    model: {idle_mu: 3.0, idle_sigma: 0.4, active_mu: 3.0, active_sigma: 0.4}
    commands:
      agent_kill: "ssh {host} taskkill /F /IM llama-server.exe"
    """
)


def test_load_standalone(tmp_path: Path) -> None:
    path = tmp_path / "churn.yaml"
    path.write_text(_STANDALONE, encoding="utf-8")
    section = load_churn_section(path)
    assert [a.name for a in section.agents] == ["mac", "win"]
    assert section.agents[1].bench_port == 9500
    assert section.commands[ChurnKind.AGENT_KILL].startswith("ssh")
    assert section.verify.enabled is True  # default


def test_embedded_under_churn_key() -> None:
    embedded = {
        "arm": "round_robin",  # a sibling B1 key we must ignore
        "churn": {
            "agents": [{"name": "mac", "host": "10.0.0.1"}],
            "duration_s": 120.0,
            "seed": 3,
            "model": {"idle_mu": 2.0, "idle_sigma": 0.3, "active_mu": 2.0, "active_sigma": 0.3},
        },
    }
    section = parse_churn_section(embedded)
    assert section.seed == 3
    assert section.agents[0].host == "10.0.0.1"


def test_scripted_events_round_trip() -> None:
    raw = {
        "agents": [{"name": "mac", "host": "10.0.0.1"}],
        "duration_s": 60.0,
        "seed": 1,
        "model": {"idle_mu": 1.0, "idle_sigma": 0.1, "active_mu": 1.0, "active_sigma": 0.1},
        "scripted": [
            {"t_offset_s": 2.0, "agent_name": "mac", "kind": "user_return"},
            {"t_offset_s": 1.0, "agent_name": "mac", "kind": "net_drop"},
        ],
    }
    section = parse_churn_section(raw)
    schedule = resolve_schedule(section)
    assert [e.t_offset_s for e in schedule] == [1.0, 2.0]
    assert schedule[0].kind is ChurnKind.NET_DROP


def test_unknown_field_is_rejected() -> None:
    raw = {
        "agents": [{"name": "mac", "host": "10.0.0.1"}],
        "duration_s": 60.0,
        "seed": 1,
        "model": {"idle_mu": 1.0, "idle_sigma": 0.1, "active_mu": 1.0, "active_sigma": 0.1},
        "bogus": 1,
    }
    with pytest.raises(ValueError):
        parse_churn_section(raw)


def test_non_mapping_rejected() -> None:
    with pytest.raises(ValueError):
        parse_churn_section([1, 2, 3])
