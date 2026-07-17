"""Row 10 (docs section 8.9): the gateway log carries metadata only.

Drives the real log record and the real JSONL writer. The record's field set is
the contract: it holds a prompt-length count, never prompt text. This asserts a
line written for a request whose prompt contains a secret leaks neither the
prompt nor any content or identity field.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fallow_coordinator.gateway.jsonl_log import JsonlRequestLog
from fallow_coordinator.gateway.logentry import GatewayLogEntry, LogStatus

SECRET_PROMPT = "sk-live-abc123 patient Jane Doe diagnosis summary"

FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "prompt_text",
        "messages",
        "response",
        "content",
        "document",
        "documents",
        "text",
        "secret",
        "api_key",
        "authorization",
        "token",
        "user",
        "user_id",
        "email",
    }
)


def _entry() -> GatewayLogEntry:
    now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC)
    return GatewayLogEntry(
        client_key_name="lab-key",
        model_id="qwen2.5-7b-instruct-q4km",
        agent_id="agent-1",
        t_submit=now,
        t_done=now,
        status=LogStatus.SERVED,
        prompt_chars=len(SECRET_PROMPT),
    )


def test_log_line_has_only_metadata_fields(tmp_path: Path) -> None:
    log_path = tmp_path / "gateway.jsonl"
    JsonlRequestLog(log_path).log(_entry())

    line = log_path.read_text(encoding="utf-8").strip()
    record = json.loads(line)

    assert set(record) == set(GatewayLogEntry.model_fields)
    assert FORBIDDEN_KEYS.isdisjoint(record)


def test_log_line_records_prompt_length_not_prompt_text(tmp_path: Path) -> None:
    log_path = tmp_path / "gateway.jsonl"
    JsonlRequestLog(log_path).log(_entry())

    line = log_path.read_text(encoding="utf-8").strip()
    record = json.loads(line)

    assert record["prompt_chars"] == len(SECRET_PROMPT)
    assert SECRET_PROMPT not in line
    assert "sk-live-abc123" not in line
