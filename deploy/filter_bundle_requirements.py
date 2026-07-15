"""Select target-specific requirements from a frozen uv export."""

from __future__ import annotations

import argparse
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement


def select_requirements(source: Path, target: str) -> list[str]:
    environment: dict[str, str] = dict(default_environment())
    environment.update(
        {
            "python_full_version": "3.12.0",
            "python_version": "3.12",
            "implementation_name": "cpython",
            "platform_python_implementation": "CPython",
        }
    )
    if target == "macos-arm64":
        environment.update(
            {
                "os_name": "posix",
                "platform_machine": "arm64",
                "platform_system": "Darwin",
                "sys_platform": "darwin",
            }
        )
    elif target == "windows-x64":
        environment.update(
            {
                "os_name": "nt",
                "platform_machine": "AMD64",
                "platform_system": "Windows",
                "sys_platform": "win32",
            }
        )
    else:  # pragma: no cover - command line validation prevents this
        raise ValueError(f"unsupported target: {target}")

    selected: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        requirement = Requirement(stripped)
        if requirement.marker is None or requirement.marker.evaluate(environment):
            requirement.marker = None
            selected.append(str(requirement))
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--target", choices=("macos-arm64", "windows-x64"), required=True)
    arguments = parser.parse_args()
    lines = select_requirements(arguments.source, arguments.target)
    arguments.output.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


if __name__ == "__main__":
    main()
