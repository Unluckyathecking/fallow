"""Job → content-addressed work-unit splitter (module I1).

A submitted :class:`JobSubmit` is split here into idempotent
:class:`WorkUnitSpec`s at submit time (ADR 005). Unit inputs are written
**content-addressed** into ``config.unit_input_dir`` (filename = sha256 of the
input bytes), and each ``work_unit_id`` is derived purely from
``sha256(model_id ‖ chunker_version ‖ input_hash)`` — so re-submitting the same
corpus produces the same ids and the queue's dedup short-circuits instantly.

Supported ``payload_ref`` shapes (v0.1):

* ``embed`` — a ``.jsonl`` file of ``{"id", "text"}`` lines, **or** a directory of
  text files. Chunks are grouped ``chunks_per_unit`` per unit; a unit's input is a
  JSON array of the chunk strings.
* ``transcribe`` — a directory of already-segmented audio files, one unit per
  file; a unit's input is the raw file bytes.

Any other ``payload_ref`` (missing path, wrong shape, unsupported kind) raises
:class:`ChunkError`, which the admin route surfaces as HTTP 422.
"""

from __future__ import annotations

import json
from hashlib import sha256
from itertools import batched
from pathlib import Path

from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import JobSubmit, WorkUnitSpec

# Bumping this invalidates every previously-derived work_unit_id on purpose.
CHUNKER_VERSION = "1"


class ChunkError(ValueError):
    """Raised when a ``payload_ref`` cannot be split into work units."""


def chunk_job(job: JobSubmit, unit_input_dir: Path, chunks_per_unit: int) -> list[WorkUnitSpec]:
    """Split ``job`` into content-addressed units, writing their inputs to disk."""
    payload = Path(job.payload_ref)
    if job.kind == WorkerKind.EMBED:
        return _chunk_embed(job, payload, unit_input_dir, chunks_per_unit)
    if job.kind == WorkerKind.TRANSCRIBE:
        return _chunk_transcribe(job, payload, unit_input_dir)
    raise ChunkError(f"unsupported job kind for batch submission: {job.kind.value}")


def _chunk_embed(
    job: JobSubmit, payload: Path, unit_input_dir: Path, chunks_per_unit: int
) -> list[WorkUnitSpec]:
    texts = _load_embed_texts(payload)
    if not texts:
        raise ChunkError(f"embed corpus is empty: {payload}")
    units: list[WorkUnitSpec] = []
    for idx, group in enumerate(batched(texts, chunks_per_unit)):
        blob = json.dumps(list(group)).encode("utf-8")
        units.append(_store_unit(job.model_id, blob, idx, unit_input_dir))
    return units


def _chunk_transcribe(job: JobSubmit, payload: Path, unit_input_dir: Path) -> list[WorkUnitSpec]:
    if not payload.is_dir():
        raise ChunkError(f"transcribe payload_ref must be a directory: {payload}")
    files = sorted(p for p in payload.iterdir() if p.is_file())
    if not files:
        raise ChunkError(f"transcribe directory has no files: {payload}")
    return [
        _store_unit(job.model_id, path.read_bytes(), idx, unit_input_dir)
        for idx, path in enumerate(files)
    ]


def _load_embed_texts(payload: Path) -> list[str]:
    if payload.is_file():
        if payload.suffix != ".jsonl":
            raise ChunkError(f"embed file payload_ref must be .jsonl: {payload}")
        return _read_jsonl_texts(payload)
    if payload.is_dir():
        return [p.read_text(encoding="utf-8") for p in sorted(payload.iterdir()) if p.is_file()]
    raise ChunkError(f"payload_ref path does not exist: {payload}")


def _read_jsonl_texts(payload: Path) -> list[str]:
    texts: list[str] = []
    for lineno, line in enumerate(payload.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
            texts.append(str(obj["text"]))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ChunkError(f"malformed embed line {lineno} in {payload}: {exc}") from exc
    return texts


def _store_unit(model_id: str, blob: bytes, idx: int, unit_input_dir: Path) -> WorkUnitSpec:
    input_hash = sha256(blob).hexdigest()
    target = unit_input_dir / input_hash
    if not target.exists():
        target.write_bytes(blob)
    seed = f"{model_id}{CHUNKER_VERSION}{input_hash}".encode()
    work_unit_id = sha256(seed).hexdigest()
    return WorkUnitSpec(work_unit_id=work_unit_id, idx=idx, input_ref=input_hash)
