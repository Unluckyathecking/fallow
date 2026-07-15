"""``TranscribeWorker``: speech-to-text via faster-whisper (optional extra).

faster-whisper is heavy and only present with the ``[whisper]`` extra, so it is
imported lazily behind a seam. If it is missing, the worker raises
:class:`WorkerUnavailableError` at CONSTRUCTION — never at run time — so the
assembly can leave the ``transcribe`` kind out of the runner.

The model is loaded once per worker instance (cached inside the injected
transcribe closure). CPU-bound decoding blocks the calling task; the assembly is
expected to run this worker on a dedicated task/thread (see the module README).
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fallow_agent.workers.config import TranscribeConfig
from fallow_agent.workers.errors import WorkerUnavailableError
from fallow_agent.workers.types import TranscribeFn, TranscriptSegment, WorkOutput
from fallow_protocol.messages import WorkMetrics, WorkUnitLease

# Builds the transcribe closure from config; may raise WorkerUnavailableError.
WhisperLoader = Callable[[TranscribeConfig], TranscribeFn]


def _import_faster_whisper() -> Any:
    """Import seam for the optional dependency (monkeypatched in tests)."""
    import faster_whisper  # type: ignore[import-not-found]

    return faster_whisper


def default_whisper_loader(config: TranscribeConfig) -> TranscribeFn:
    """Load a faster-whisper model and adapt it to :data:`TranscribeFn`.

    Raises :class:`WorkerUnavailableError` if the extra is not installed.
    """
    try:
        faster_whisper = _import_faster_whisper()
    except ImportError as exc:
        raise WorkerUnavailableError(
            "faster-whisper is not installed; install the 'whisper' extra to "
            "enable the transcribe worker"
        ) from exc

    model = faster_whisper.WhisperModel(
        config.model_size_or_path,
        device=config.device,
        compute_type=config.compute_type,
    )

    def _transcribe(audio_path: Path) -> list[TranscriptSegment]:
        segments, _info = model.transcribe(str(audio_path), beam_size=config.beam_size)
        return [(float(s.start), float(s.end), str(s.text)) for s in segments]

    return _transcribe


class TranscribeWorker:
    """Transcribes one audio segment to text + timestamped segments."""

    def __init__(
        self,
        *,
        config: TranscribeConfig,
        tmp_dir: Path,
        loader: WhisperLoader = default_whisper_loader,
    ) -> None:
        self._config = config
        self._tmp_dir = Path(tmp_dir)
        # Load (and cache) the model now so an unavailable backend fails here.
        self._transcribe = loader(config)

    async def run(self, lease: WorkUnitLease, input_bytes: bytes) -> WorkOutput:
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        audio_path = self._tmp_dir / f"{lease.work_unit_id}{self._config.audio_suffix}"
        try:
            audio_path.write_bytes(input_bytes)
            segments = list(self._transcribe(audio_path))
        finally:
            audio_path.unlink(missing_ok=True)
        payload = _encode_transcript(segments)
        metrics = WorkMetrics(duration_s=0.0, items=len(segments))
        return WorkOutput(payload=payload, metrics=metrics)


def _encode_transcript(segments: list[TranscriptSegment]) -> bytes:
    text = " ".join(seg[2].strip() for seg in segments).strip()
    document = {
        "text": text,
        "segments": [
            {"start": start, "end": end, "text": seg_text} for (start, end, seg_text) in segments
        ],
    }
    return json.dumps(document, separators=(",", ":")).encode("utf-8")
