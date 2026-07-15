"""JsonlRequestLog writes one round-trippable JSON line per entry."""

import json
from datetime import UTC, datetime
from pathlib import Path

from fallow_coordinator.gateway import AffinityState, GatewayLogEntry, JsonlRequestLog, LogStatus


def _entry(status: LogStatus, model: str) -> GatewayLogEntry:
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
    return GatewayLogEntry(
        client_key_name="team-a",
        model_id=model,
        agent_id="agent-1",
        t_submit=now,
        t_first_byte=now,
        t_done=now,
        status=status,
        retried=False,
        prompt_chars=12,
        affinity=AffinityState.HIT,
    )


def test_appends_one_line_per_entry(tmp_path: Path) -> None:
    path = tmp_path / "requests.jsonl"
    log = JsonlRequestLog(path)

    log.log(_entry(LogStatus.SERVED, "qwen2.5-7b"))
    log.log(_entry(LogStatus.SHED, "bge-small"))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["status"] == "served"
    assert first["model_id"] == "qwen2.5-7b"
    assert first["affinity"] == "hit"
    assert json.loads(lines[1])["status"] == "shed"


def test_round_trips_through_the_model(tmp_path: Path) -> None:
    path = tmp_path / "requests.jsonl"
    JsonlRequestLog(path).log(_entry(LogStatus.SERVED, "qwen2.5-7b"))

    line = path.read_text(encoding="utf-8").strip()
    restored = GatewayLogEntry.model_validate_json(line)
    assert restored.status is LogStatus.SERVED
    assert restored.prompt_chars == 12
    assert restored.affinity is AffinityState.HIT
