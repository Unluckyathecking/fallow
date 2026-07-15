"""fallow_protocol must import nothing beyond pydantic + stdlib.

This is the portability contract for the future Go/Rust port and the
precondition for installing the protocol package on minimal worker machines.
(import-linter enforces a named blocklist; this test enforces the allowlist.)
"""

import ast
import sys
from pathlib import Path

import fallow_protocol

ALLOWED_TOP_LEVEL = {"pydantic", "fallow_protocol", *sys.stdlib_module_names}


def iter_source_files():
    pkg_dir = Path(fallow_protocol.__file__).parent
    return sorted(pkg_dir.rglob("*.py"))


def test_only_pydantic_and_stdlib_imports():
    offenders = []
    for path in iter_source_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:  # relative import, inside the package
                    continue
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top not in ALLOWED_TOP_LEVEL:
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, f"non-stdlib/pydantic imports in fallow_protocol: {offenders}"
