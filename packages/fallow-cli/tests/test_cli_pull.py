"""``hf:`` source parsing, the curated catalog, and ``flw models pull`` resolution.

Offline: downloads run through an injected ``httpx.MockTransport`` serving a
hand-built GGUF header. The one live test is opt-in via ``FALLOW_LIVE_HF_TEST``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from cli_helpers import COORD_URL, bytes_transport, recording_transport, sample_gguf
from pytest import MonkeyPatch
from typer.testing import CliRunner

from fallow_cli import blobs, catalog, hf, main, pull
from fallow_cli.errors import CliError
from fallow_protocol import WorkerKind

SMALLEST_CATALOG_ID = "nomic-embed-text-v1.5-q4km"


def _invoke(runner: CliRunner, env: dict[str, str], args: list[str]) -> object:
    return runner.invoke(main.app, ["--coordinator-url", COORD_URL, *args], env=env)


# ── hf: source parsing ───────────────────────────────────────────────────────
def test_hf_spec_resolves_to_canonical_url() -> None:
    source = hf.parse("hf:Qwen/Qwen2.5-0.5B-Instruct-GGUF/qwen2.5-0.5b-instruct-q4_k_m.gguf")
    assert source.revision == "main"
    assert source.url == (
        "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/"
        "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    )


def test_hf_spec_honours_revision_and_subdirectory() -> None:
    source = hf.parse("hf:owner/repo/Q4_K_M/split-00001-of-00002.gguf@abc123")
    assert source.revision == "abc123"
    assert source.file_path == "Q4_K_M/split-00001-of-00002.gguf"
    assert source.url.endswith("/resolve/abc123/Q4_K_M/split-00001-of-00002.gguf")


@pytest.mark.parametrize(
    "spec",
    [
        "hf:owner/repo",  # no file
        "hf:owner/../secrets/x.gguf",  # traversal
        "hf:owner/repo/x.gguf?token=1",  # query
        "hf:owner/repo/x.gguf#frag",  # fragment
        "hf:owner/repo/x.gguf@rev@rev",  # second revision
        "hf:owner/repo/model.bin",  # not a GGUF
        "hf:/repo/x.gguf",  # empty owner
        "hf:owner/repo/.hidden.gguf",  # leading dot
        "https://host/x.gguf",  # not an hf: spec at all
    ],
)
def test_hf_spec_rejections(spec: str) -> None:
    with pytest.raises(ValueError):
        hf.parse(spec)


# ── catalog ──────────────────────────────────────────────────────────────────
def test_packaged_catalog_loads_and_every_source_resolves() -> None:
    entries = catalog.load_catalog()
    assert len(entries) >= 3
    assert len({entry.id for entry in entries}) == len(entries)
    for entry in entries:
        assert entry.url.startswith("https://huggingface.co/")
        assert entry.min_vram_mb == 0
        assert entry.license


def test_catalog_unknown_id_lists_known_ids() -> None:
    with pytest.raises(CliError) as exc:
        catalog.find("no-such-model")
    assert "unknown catalog model" in exc.value.message
    assert SMALLEST_CATALOG_ID in exc.value.message


def test_catalog_rejects_unknown_field(tmp_path: Path) -> None:
    bad = tmp_path / "catalog.toml"
    bad.write_text('[[model]]\nid = "x"\nnonsense = 1\n', encoding="utf-8")
    with pytest.raises(CliError) as exc:
        catalog.load_catalog(bad)
    assert "unreadable" in exc.value.message


def test_catalog_rejects_bad_source(tmp_path: Path) -> None:
    bad = tmp_path / "catalog.toml"
    bad.write_text(_catalog_toml(source="hf:owner/repo"), encoding="utf-8")
    with pytest.raises(CliError) as exc:
        catalog.load_catalog(bad)
    assert "catalog entry demo" in exc.value.message


def _catalog_toml(*, source: str = "hf:owner/repo/demo.gguf", sha256: str = "") -> str:
    return (
        "[[model]]\n"
        'id = "demo"\n'
        f'source = "{source}"\n'
        'family = "demo-family"\n'
        'quant = "Q5_K_M"\n'
        'worker_kind = "embed"\n'
        f'sha256 = "{sha256}"\n'
        "size_bytes = 1024\n"
        "min_ram_mb = 777\n"
        "min_vram_mb = 0\n"
        'license = "apache-2.0"\n'
        'note = "a fixture"\n'
    )


# ── plan_source ──────────────────────────────────────────────────────────────
def test_plan_source_passes_plain_urls_through() -> None:
    plan = pull.plan_source("http://host/m.gguf", None)
    assert plan.url == "http://host/m.gguf"
    assert plan.entry is None
    assert plan.expected_sha256 is None


def test_plan_source_rejects_both_and_neither() -> None:
    with pytest.raises(CliError, match="not both"):
        pull.plan_source("http://host/m.gguf", "some-id")
    with pytest.raises(CliError, match="--catalog"):
        pull.plan_source(None, None)


def test_plan_source_reports_bad_hf_spec_as_cli_error() -> None:
    with pytest.raises(CliError, match=r"must name a \.gguf file"):
        pull.plan_source("hf:owner/repo/model.bin", None)


# ── resolve_fields ───────────────────────────────────────────────────────────
def _blob(tmp_path: Path, payload: bytes | None = None) -> Path:
    path = tmp_path / "m.gguf"
    path.write_bytes(payload if payload is not None else sample_gguf())
    return path


def test_fields_derive_quant_and_min_ram_from_the_file(tmp_path: Path) -> None:
    path = _blob(tmp_path)
    plan = pull.plan_source("http://host/m.gguf", None)
    fields = pull.resolve_fields(plan, path, pull.Overrides(model_id="m", family="f"))
    assert fields.quant == "Q4_K_M"
    assert fields.min_ram_mb == 512 + 1  # a header-sized file rounds up to 1 MiB
    assert fields.min_vram_mb == 0
    assert fields.worker_kind is WorkerKind.CHAT
    assert fields.license is None


def test_flags_win_over_derived_and_over_the_catalog(tmp_path: Path) -> None:
    path = _blob(tmp_path)
    entry = catalog.load_catalog(_written(tmp_path, _catalog_toml()))[0]
    plan = pull.PullPlan(url=entry.url, origin=entry.source, entry=entry)
    fields = pull.resolve_fields(
        plan,
        path,
        pull.Overrides(
            model_id="mine",
            family="my-family",
            quant="Q8_0",
            worker_kind=WorkerKind.TRANSCRIBE,
            min_ram_mb=99,
            min_vram_mb=4096,
        ),
    )
    assert (fields.model_id, fields.family, fields.quant) == ("mine", "my-family", "Q8_0")
    assert fields.worker_kind is WorkerKind.TRANSCRIBE
    assert (fields.min_ram_mb, fields.min_vram_mb) == (99, 4096)
    assert fields.license == "apache-2.0"


def test_catalog_wins_over_the_file(tmp_path: Path) -> None:
    path = _blob(tmp_path)
    entry = catalog.load_catalog(_written(tmp_path, _catalog_toml()))[0]
    plan = pull.PullPlan(url=entry.url, origin=entry.source, entry=entry)
    fields = pull.resolve_fields(plan, path, pull.Overrides())
    assert (fields.model_id, fields.family, fields.quant) == ("demo", "demo-family", "Q5_K_M")
    assert fields.worker_kind is WorkerKind.EMBED
    assert fields.min_ram_mb == 777


def test_unparseable_file_falls_back_to_flags(tmp_path: Path) -> None:
    path = _blob(tmp_path, b"NOT-A-GGUF-FILE" * 8)
    plan = pull.plan_source("http://host/m.gguf", None)
    fields = pull.resolve_fields(
        plan, path, pull.Overrides(model_id="m", family="f", quant="Q4_K_M")
    )
    assert fields.quant == "Q4_K_M"


def test_unparseable_file_without_quant_flag_says_why(tmp_path: Path) -> None:
    path = _blob(tmp_path, b"NOT-A-GGUF-FILE" * 8)
    plan = pull.plan_source("http://host/m.gguf", None)
    with pytest.raises(CliError) as exc:
        pull.resolve_fields(plan, path, pull.Overrides(model_id="m", family="f"))
    assert "not a GGUF file" in exc.value.message
    assert "pass --quant" in exc.value.message


def test_missing_model_id_and_family_are_named(tmp_path: Path) -> None:
    path = _blob(tmp_path)
    plan = pull.plan_source("http://host/m.gguf", None)
    with pytest.raises(CliError, match="--model-id"):
        pull.resolve_fields(plan, path, pull.Overrides())
    with pytest.raises(CliError, match="--family"):
        pull.resolve_fields(plan, path, pull.Overrides(model_id="m"))


def _written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "catalog.toml"
    path.write_text(text, encoding="utf-8")
    return path


# ── end-to-end through the CLI ───────────────────────────────────────────────
def test_pull_hf_spec_records_the_canonical_url(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    payload = sample_gguf() + b"\x00" * 4096
    store: dict[str, object] = {}
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(main, "_DOWNLOAD_TRANSPORT", bytes_transport(payload))
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", recording_transport(store, status=201))

    result = _invoke(
        runner,
        env,
        [
            "models",
            "pull",
            "hf:Qwen/Qwen2.5-0.5B-Instruct-GGUF/qwen2.5-0.5b-instruct-q4_k_m.gguf",
            "--model-id",
            "qwen-small",
            "--family",
            "qwen2.5",
        ],
    )
    assert result.exit_code == 0, result.output
    body = store["body"]
    assert isinstance(body, dict)
    manifest = body["manifest"]
    assert manifest["source_url"] == (
        "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/"
        "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    )
    assert manifest["quant"] == "Q4_K_M"  # derived from the header
    assert manifest["min_ram_mb"] == 513  # derived from the size
    assert manifest["min_vram_mb"] == 0
    assert "registered qwen-small from hf:Qwen/" in result.output


def test_pull_catalog_applies_metadata_and_verifies_sha256(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    payload = sample_gguf()
    entry_toml = _catalog_toml(sha256=hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(catalog, "_packaged_bytes", lambda: entry_toml.encode("utf-8"))
    store: dict[str, object] = {}
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(main, "_DOWNLOAD_TRANSPORT", bytes_transport(payload))
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", recording_transport(store, status=201))

    result = _invoke(runner, env, ["models", "pull", "--catalog", "demo"])
    assert result.exit_code == 0, result.output
    body = store["body"]
    assert isinstance(body, dict)
    manifest = body["manifest"]
    assert manifest["model_id"] == "demo"
    assert manifest["family"] == "demo-family"
    assert manifest["quant"] == "Q5_K_M"
    assert manifest["worker_kind"] == "embed"
    assert manifest["min_ram_mb"] == 777
    assert manifest["license"] == "apache-2.0"
    assert manifest["source_url"].endswith("/resolve/main/demo.gguf")


def test_pull_catalog_refuses_a_sha256_mismatch(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    entry_toml = _catalog_toml(sha256="ab" * 32)
    monkeypatch.setattr(catalog, "_packaged_bytes", lambda: entry_toml.encode("utf-8"))
    store: dict[str, object] = {}
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(main, "_DOWNLOAD_TRANSPORT", bytes_transport(sample_gguf()))
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", recording_transport(store, status=201))

    result = _invoke(runner, env, ["models", "pull", "--catalog", "demo"])
    assert result.exit_code == 1
    assert "sha256 mismatch" in result.output
    assert "body" not in store  # nothing was registered
    assert not (tmp_path / "blobs" / "demo.gguf").exists()  # the bad blob is gone


def test_pull_unknown_catalog_id_exits_cleanly(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    result = _invoke(runner, env, ["models", "pull", "--catalog", "nope"])
    assert result.exit_code == 1
    assert "unknown catalog model" in result.output


# ── opt-in live test ─────────────────────────────────────────────────────────
@pytest.mark.skipif(
    os.environ.get("FALLOW_LIVE_HF_TEST") != "1",
    reason="live Hugging Face download; set FALLOW_LIVE_HF_TEST=1 to run it",
)
def test_live_pull_of_the_smallest_catalog_entry(
    runner: CliRunner, env: dict[str, str], monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """THE ONLY TEST HERE THAT DIALS THE INTERNET. Downloads ~80 MB."""
    entry = catalog.find(SMALLEST_CATALOG_ID)
    store: dict[str, object] = {}
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", recording_transport(store, status=201))

    result = _invoke(runner, env, ["models", "pull", "--catalog", entry.id])
    assert result.exit_code == 0, result.output
    body = store["body"]
    assert isinstance(body, dict)
    assert body["manifest"]["sha256"] == entry.sha256
    assert body["manifest"]["size_bytes"] == entry.size_bytes
    assert body["manifest"]["quant"] == entry.quant
    assert (tmp_path / "blobs" / "nomic-embed-text-v1.5.Q4_K_M.gguf").is_file()
