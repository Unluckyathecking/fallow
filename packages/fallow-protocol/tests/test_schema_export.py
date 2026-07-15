"""Schema export is deterministic and the committed /schemas dir is current."""

from pathlib import Path

from fallow_protocol import WIRE_TYPES
from fallow_protocol.export_schemas import export_schemas

REPO_SCHEMAS = Path(__file__).parents[3] / "schemas"


def test_export_writes_one_schema_per_wire_type(tmp_path):
    written = export_schemas(tmp_path)
    assert len(written) == len(WIRE_TYPES)
    assert all(p.exists() and p.stat().st_size > 0 for p in written)


def test_export_is_deterministic(tmp_path):
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    for p_a, p_b in zip(export_schemas(a_dir), export_schemas(b_dir), strict=True):
        assert p_a.read_text() == p_b.read_text()


def test_committed_schemas_are_current(tmp_path):
    """Fails when wire types change without re-running export_schemas.
    Fix: uv run python -m fallow_protocol.export_schemas schemas/"""
    fresh = {p.name: p.read_text() for p in export_schemas(tmp_path)}
    committed = {p.name: p.read_text() for p in REPO_SCHEMAS.glob("*.json")}
    assert committed == fresh
