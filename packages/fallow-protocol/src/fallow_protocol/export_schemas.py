"""Export every wire type as JSON Schema into /schemas.

The generated schemas are committed and diffed in CI: any change to the wire
protocol shows up as a schema diff in review, and they double as the
language-neutral spec for the future Go/Rust port.

Usage: python -m fallow_protocol.export_schemas [outdir]
"""

import json
import sys
from pathlib import Path

from pydantic import BaseModel

from fallow_protocol import WIRE_TYPES


def export_schemas(outdir: Path) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for model in WIRE_TYPES:
        schema = model.model_json_schema()
        path = outdir / f"{model.__name__}.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


def _is_wire_type(obj: object) -> bool:
    return isinstance(obj, type) and issubclass(obj, BaseModel)


def main(argv: list[str]) -> int:
    outdir = Path(argv[1]) if len(argv) > 1 else Path("schemas")
    written = export_schemas(outdir)
    print(f"wrote {len(written)} schemas to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
