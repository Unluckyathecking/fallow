"""OCR chunking: a directory of page images becomes one self-contained unit each."""

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

import pytest

from fallow_coordinator.app.chunker import (
    CHUNKER_VERSION,
    OCR_PROMPT_VERSION,
    ChunkError,
    _store_unit,
    chunk_job,
)
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import JobSubmit

_MODEL = "vlm-1"


def _job(payload: Path, model_id: str = _MODEL) -> JobSubmit:
    return JobSubmit(kind=WorkerKind.OCR, model_id=model_id, payload_ref=str(payload))


def _write_pages(tmp_path: Path, n: int) -> Path:
    corpus = tmp_path / "pages"
    corpus.mkdir()
    for i in range(n):
        (corpus / f"page-{i:02d}.png").write_bytes(b"\x89PNG-fake-" + bytes([i]) * 20)
    return corpus


def test_ocr_one_unit_per_image_in_name_order(tmp_path: Path) -> None:
    corpus = _write_pages(tmp_path, 3)
    inputs = tmp_path / "inputs"
    inputs.mkdir()

    units = chunk_job(_job(corpus), inputs, chunks_per_unit=4)

    assert [u.idx for u in units] == [0, 1, 2]
    for i, unit in enumerate(units):
        blob = (inputs / unit.input_ref).read_bytes()
        assert unit.input_ref == sha256(blob).hexdigest()
        document = json.loads(blob)
        assert document["schema"] == "ocr-unit/1"
        assert document["prompt_version"] == OCR_PROMPT_VERSION
        image = (corpus / f"page-{i:02d}.png").read_bytes()
        assert base64.b64decode(document["image_b64"]) == image


def test_ocr_unit_id_follows_documented_derivation(tmp_path: Path) -> None:
    corpus = _write_pages(tmp_path, 1)
    inputs = tmp_path / "inputs"
    inputs.mkdir()

    (unit,) = chunk_job(_job(corpus), inputs, chunks_per_unit=1)

    seed = f"{_MODEL}{CHUNKER_VERSION}{unit.input_ref}".encode()
    assert unit.work_unit_id == sha256(seed).hexdigest()


def test_ocr_unit_ids_stable_across_resubmits(tmp_path: Path) -> None:
    corpus = _write_pages(tmp_path, 4)
    inputs = tmp_path / "inputs"
    inputs.mkdir()

    first = chunk_job(_job(corpus), inputs, chunks_per_unit=1)
    second = chunk_job(_job(corpus), inputs, chunks_per_unit=1)

    assert [u.work_unit_id for u in first] == [u.work_unit_id for u in second]


def test_ocr_unit_ids_depend_on_model(tmp_path: Path) -> None:
    corpus = _write_pages(tmp_path, 2)
    inputs = tmp_path / "inputs"
    inputs.mkdir()

    ids_a = {u.work_unit_id for u in chunk_job(_job(corpus), inputs, chunks_per_unit=1)}
    ids_b = {u.work_unit_id for u in chunk_job(_job(corpus, "vlm-2"), inputs, chunks_per_unit=1)}

    assert ids_a.isdisjoint(ids_b)


def test_ocr_changed_page_changes_only_its_unit(tmp_path: Path) -> None:
    corpus = _write_pages(tmp_path, 3)
    inputs = tmp_path / "inputs"
    inputs.mkdir()

    before = chunk_job(_job(corpus), inputs, chunks_per_unit=1)
    (corpus / "page-01.png").write_bytes(b"\x89PNG-rescanned")
    after = chunk_job(_job(corpus), inputs, chunks_per_unit=1)

    assert before[0].work_unit_id == after[0].work_unit_id
    assert before[1].work_unit_id != after[1].work_unit_id
    assert before[2].work_unit_id == after[2].work_unit_id


def test_ocr_identical_pages_stay_distinct_units(tmp_path: Path) -> None:
    """Two byte-identical pages (repeated blanks) must not collapse into one unit."""
    corpus = tmp_path / "pages"
    corpus.mkdir()
    blank = b"\x89PNG-blank" + b"\x00" * 32
    (corpus / "src-p00000.png").write_bytes(blank)
    (corpus / "src-p00001.png").write_bytes(blank)
    inputs = tmp_path / "inputs"
    inputs.mkdir()

    units = chunk_job(_job(corpus), inputs, chunks_per_unit=1)

    assert [u.idx for u in units] == [0, 1]
    assert units[0].work_unit_id != units[1].work_unit_id
    pages = [json.loads((inputs / u.input_ref).read_bytes())["page"] for u in units]
    assert pages == ["src-p00000.png", "src-p00001.png"]


def test_ocr_ignores_non_image_files(tmp_path: Path) -> None:
    """`flw ocr prepare` writes corpus.json beside the pages; it is not a page."""
    corpus = _write_pages(tmp_path, 2)
    (corpus / "corpus.json").write_text('{"dpi": 200, "sources": []}', encoding="utf-8")
    (corpus / "notes.txt").write_text("stray file", encoding="utf-8")
    # Container formats never appear in prepared corpora; a raw dir with one is
    # skipped rather than sent to a decoder that reads a single frame.
    (corpus / "raw-scan.tiff").write_bytes(b"II*\x00-multi-frame")
    inputs = tmp_path / "inputs"
    inputs.mkdir()

    units = chunk_job(_job(corpus), inputs, chunks_per_unit=1)

    assert len(units) == 2


def test_ocr_rejects_directory_with_no_image_files(tmp_path: Path) -> None:
    corpus = tmp_path / "pages"
    corpus.mkdir()
    (corpus / "corpus.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ChunkError):
        chunk_job(_job(corpus), tmp_path / "inputs", chunks_per_unit=1)


def test_ocr_rejects_file_payload(tmp_path: Path) -> None:
    single = tmp_path / "page.png"
    single.write_bytes(b"\x89PNG")
    with pytest.raises(ChunkError):
        chunk_job(_job(single), tmp_path / "inputs", chunks_per_unit=1)


def test_ocr_rejects_empty_directory(tmp_path: Path) -> None:
    corpus = tmp_path / "empty"
    corpus.mkdir()
    with pytest.raises(ChunkError):
        chunk_job(_job(corpus), tmp_path / "inputs", chunks_per_unit=1)


def test_ocr_rejects_missing_payload(tmp_path: Path) -> None:
    with pytest.raises(ChunkError):
        chunk_job(_job(tmp_path / "absent"), tmp_path / "inputs", chunks_per_unit=1)


def test_store_unit_publishes_atomically_under_concurrency(tmp_path: Path) -> None:
    """Submissions now chunk on worker threads, so two sharing an input write the
    same content-addressed file at once. Each write must publish the unit whole —
    never a half-written file a reader could fetch — and leave no temp behind."""
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    blob = b"\x89PNG-fake-" + b"z" * 4096
    input_hash = sha256(blob).hexdigest()
    barrier = threading.Barrier(8)

    def store() -> str:
        barrier.wait()  # release all writers together to force overlap
        return _store_unit("m", blob, 0, unit_dir).input_ref

    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = [f.result() for f in [pool.submit(store) for _ in range(8)]]

    assert set(refs) == {input_hash}
    # Only the published target remains — no leftover ".<hash>.<uuid>.tmp" files.
    assert [p.name for p in unit_dir.iterdir()] == [input_hash]
    assert (unit_dir / input_hash).read_bytes() == blob


def test_store_unit_leaves_no_temp_when_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails mid-publish (disk full, I/O error) must not strand a
    hidden temp file in the input dir; the finally always clears it."""
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()

    real_write = Path.write_bytes

    def boom(self: Path, data: bytes) -> int:
        real_write(self, data)  # the temp file is created on disk...
        raise OSError("disk full")  # ...then the write fails partway

    monkeypatch.setattr(Path, "write_bytes", boom)
    with pytest.raises(OSError):
        _store_unit("m", b"payload", 0, unit_dir)

    assert list(unit_dir.iterdir()) == []  # the created temp is cleaned up, no debris
