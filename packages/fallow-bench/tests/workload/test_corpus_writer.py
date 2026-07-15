"""Prompt-corpus loading and the JSONL writer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from workload_helpers import StepClock

from fallow_bench.workload.corpus import load_prompts
from fallow_bench.workload.records import RequestRecord, RequestStatus
from fallow_bench.workload.writer import JsonlWriter


def test_load_prompts_concatenates_and_strips(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("one\n\n  two  \n", encoding="utf-8")
    b.write_text("three\n", encoding="utf-8")
    assert load_prompts([a, b]) == ("one", "two", "three")


def test_load_prompts_empty_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no prompts"):
        load_prompts([empty])


def test_writer_roundtrip(tmp_path: Path) -> None:
    clock = StepClock()
    path = tmp_path / "sub" / "requests.jsonl"
    record = RequestRecord(
        req_id=0,
        prompt_idx=3,
        t_scheduled=clock(),
        t_submit=clock(),
        t_first_token=clock(),
        t_done=clock(),
        status=RequestStatus.OK,
        http_status=200,
        tokens_out=5,
    )
    with JsonlWriter(path) as writer:
        writer.write(record)
        writer.write(record)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["req_id"] == 0
    assert parsed["status"] == "ok"
    assert parsed["tokens_out"] == 5
