"""OCR chunking: a directory of page images becomes one self-contained unit each."""

import base64
import json
from hashlib import sha256
from pathlib import Path

import pytest

from fallow_coordinator.app.chunker import (
    CHUNKER_VERSION,
    OCR_PROMPT_VERSION,
    ChunkError,
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
