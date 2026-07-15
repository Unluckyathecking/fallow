"""Unit tests for TranscribeWorker.

The optional ``faster_whisper`` dependency is NEVER imported: the unavailable
path is tested by monkeypatching the import seam, and orchestration is tested
with a fake transcriber callable.
"""

import json
from pathlib import Path

import pytest
from workers_helpers import make_lease

from fallow_agent.workers import (
    TranscribeConfig,
    TranscribeWorker,
    WorkerUnavailableError,
    default_whisper_loader,
)
from fallow_agent.workers import transcribe as transcribe_module
from fallow_agent.workers.types import TranscriptSegment
from fallow_protocol.capabilities import WorkerKind


def _config() -> TranscribeConfig:
    return TranscribeConfig(model_size_or_path="base")


def test_transcribe_unavailable_raises_at_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _missing() -> object:
        raise ImportError("No module named 'faster_whisper'")

    monkeypatch.setattr(transcribe_module, "_import_faster_whisper", _missing)

    with pytest.raises(WorkerUnavailableError):
        TranscribeWorker(config=_config(), tmp_dir=tmp_path, loader=default_whisper_loader)


async def test_transcribe_orchestration_with_fake_seam(tmp_path: Path) -> None:
    captured: dict[str, bytes] = {}

    def fake_loader(_config: TranscribeConfig) -> object:
        def _transcribe(audio_path: Path) -> list[TranscriptSegment]:
            captured["audio"] = audio_path.read_bytes()
            return [(0.0, 1.0, " hello "), (1.0, 2.0, "world")]

        return _transcribe

    worker = TranscribeWorker(config=_config(), tmp_dir=tmp_path, loader=fake_loader)
    output = await worker.run(make_lease(kind=WorkerKind.TRANSCRIBE), b"audio-bytes")

    assert captured["audio"] == b"audio-bytes"
    payload = json.loads(output.payload)
    assert payload["text"] == "hello world"
    assert payload["segments"] == [
        {"start": 0.0, "end": 1.0, "text": " hello "},
        {"start": 1.0, "end": 2.0, "text": "world"},
    ]
    assert output.metrics.items == 2
    # The scratch audio file is removed after the run.
    assert list(tmp_path.iterdir()) == []


async def test_transcribe_cleans_up_on_failure(tmp_path: Path) -> None:
    def fake_loader(_config: TranscribeConfig) -> object:
        def _transcribe(_audio_path: Path) -> list[TranscriptSegment]:
            raise ValueError("decode failed")

        return _transcribe

    worker = TranscribeWorker(config=_config(), tmp_dir=tmp_path, loader=fake_loader)
    with pytest.raises(ValueError, match="decode failed"):
        await worker.run(make_lease(kind=WorkerKind.TRANSCRIBE), b"audio-bytes")

    # Even when transcription raises, no scratch file is left behind.
    assert list(tmp_path.iterdir()) == []
