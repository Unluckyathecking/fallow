"""``flw ocr prepare``: normalize arbitrary source documents into a page-image corpus.

Rendering and office conversion are injected seams, so no pypdfium2 and no
soffice run in tests.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fallow_cli.errors import CliError
from fallow_cli.ocr_prepare import prepare_corpus

_PAGES = [b"png-page-0", b"png-page-1"]


def _render(pdf_path: Path, dpi: int) -> list[bytes]:
    assert pdf_path.exists()
    assert dpi > 0
    return list(_PAGES)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _corpus(out_dir: Path) -> dict:
    return json.loads((out_dir / "corpus.json").read_text(encoding="utf-8"))


def test_prepare_renders_pdf_into_page_images(tmp_path: Path) -> None:
    pdf = tmp_path / "exam.pdf"
    pdf.write_bytes(b"%PDF-fake")
    out = tmp_path / "out"

    prepare_corpus([pdf], out, dpi=200, render_pdf=_render)

    sha = _sha(b"%PDF-fake")
    names = [f"{sha[:12]}-p00000.png", f"{sha[:12]}-p00001.png"]
    for name, body in zip(names, _PAGES, strict=True):
        assert (out / name).read_bytes() == body
    document = _corpus(out)
    assert document["dpi"] == 200
    assert document["sources"] == [{"file": "exam.pdf", "sha256": sha, "pages": names}]


def test_prepare_copies_images_verbatim(tmp_path: Path) -> None:
    scan = tmp_path / "scan.jpg"
    scan.write_bytes(b"\xff\xd8\xff-scan")
    out = tmp_path / "out"

    prepare_corpus([scan], out, dpi=200, render_pdf=_render)

    sha = _sha(b"\xff\xd8\xff-scan")
    name = f"{sha[:12]}-p00000.jpg"
    assert (out / name).read_bytes() == b"\xff\xd8\xff-scan"
    assert _corpus(out)["sources"] == [{"file": "scan.jpg", "sha256": sha, "pages": [name]}]


def test_prepare_converts_office_documents_via_seam(tmp_path: Path) -> None:
    doc = tmp_path / "notes.docx"
    doc.write_bytes(b"docx-bytes")
    out = tmp_path / "out"

    def _convert(source: Path, workdir: Path) -> Path:
        pdf = workdir / "notes.pdf"
        pdf.write_bytes(b"%PDF-converted")
        return pdf

    prepare_corpus([doc], out, dpi=200, render_pdf=_render, convert_to_pdf=_convert)

    # Source identity is the ORIGINAL file's hash, not the converted pdf's.
    sha = _sha(b"docx-bytes")
    document = _corpus(out)
    assert document["sources"][0]["file"] == "notes.docx"
    assert document["sources"][0]["sha256"] == sha
    assert len(document["sources"][0]["pages"]) == len(_PAGES)


def test_prepare_identical_sources_share_pages(tmp_path: Path) -> None:
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"%PDF-same")
    b.write_bytes(b"%PDF-same")
    out = tmp_path / "out"
    calls: list[Path] = []

    def _counting_render(pdf_path: Path, dpi: int) -> list[bytes]:
        calls.append(pdf_path)
        return list(_PAGES)

    prepare_corpus([a, b], out, dpi=200, render_pdf=_counting_render)

    assert len(calls) == 1  # duplicate bytes rendered once
    sources = _corpus(out)["sources"]
    assert [s["file"] for s in sources] == ["a.pdf", "b.pdf"]
    assert sources[0]["pages"] == sources[1]["pages"]


def test_prepare_expands_a_multi_frame_tiff_into_one_png_per_page(tmp_path: Path) -> None:
    from PIL import Image

    scan = tmp_path / "scan.tiff"
    first = Image.new("RGB", (4, 4), "white")
    second = Image.new("RGB", (4, 4), "black")
    first.save(scan, save_all=True, append_images=[second])
    out = tmp_path / "out"

    prepare_corpus([scan], out, dpi=200, render_pdf=_render)

    (source,) = _corpus(out)["sources"]
    assert len(source["pages"]) == 2
    for name in source["pages"]:
        assert name.endswith(".png")
        assert (out / name).read_bytes().startswith(b"\x89PNG")


def test_prepare_transcodes_bmp_to_png(tmp_path: Path) -> None:
    from PIL import Image

    scan = tmp_path / "scan.bmp"
    Image.new("RGB", (4, 4), "white").save(scan)
    out = tmp_path / "out"

    prepare_corpus([scan], out, dpi=200, render_pdf=_render)

    (source,) = _corpus(out)["sources"]
    (name,) = source["pages"]
    assert name.endswith(".png")
    assert (out / name).read_bytes().startswith(b"\x89PNG")


def test_prepare_rejects_unknown_extension(tmp_path: Path) -> None:
    weird = tmp_path / "data.xyz"
    weird.write_bytes(b"???")
    with pytest.raises(CliError) as exc:
        prepare_corpus([weird], tmp_path / "out", dpi=200, render_pdf=_render)
    assert "data.xyz" in exc.value.message


def test_prepare_rejects_empty_input_list(tmp_path: Path) -> None:
    with pytest.raises(CliError):
        prepare_corpus([], tmp_path / "out", dpi=200, render_pdf=_render)


def test_prepare_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CliError):
        prepare_corpus([tmp_path / "absent.pdf"], tmp_path / "out", dpi=200, render_pdf=_render)


def test_prepare_rejects_a_non_empty_output_directory(tmp_path: Path) -> None:
    """Stale pages from an earlier run must never mix into a new corpus."""
    pdf = tmp_path / "exam.pdf"
    pdf.write_bytes(b"%PDF-fake")
    out = tmp_path / "out"
    out.mkdir()
    (out / "leftover-p00000.png").write_bytes(b"old page")

    with pytest.raises(CliError) as exc:
        prepare_corpus([pdf], out, dpi=200, render_pdf=_render)
    assert "not empty" in exc.value.message


def test_prepare_accepts_an_existing_empty_output_directory(tmp_path: Path) -> None:
    pdf = tmp_path / "exam.pdf"
    pdf.write_bytes(b"%PDF-fake")
    out = tmp_path / "out"
    out.mkdir()

    prepare_corpus([pdf], out, dpi=200, render_pdf=_render)

    assert (out / "corpus.json").is_file()
