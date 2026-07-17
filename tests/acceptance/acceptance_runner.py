"""Print the Phase-A acceptance checklist for an IT admin.

Run it as a script to see the whole matrix: which rows CI already asserts, and
the exact commands to run by hand on a real machine or fleet for the rest.

    python tests/acceptance/acceptance_runner.py
"""

from __future__ import annotations

from acceptance_matrix import PHASE_A, Check, Mode, automated, manual


def _block(check: Check) -> str:
    return (
        f"[{check.mode.value}] {check.id} (docs/school-pilot.md §{check.doc_ref})\n"
        f"    {check.title}\n"
        f"    run:      {check.command}\n"
        f"    expected: {check.expected}\n"
    )


def format_checklist() -> str:
    lines = [
        "Fallow Phase-A pilot acceptance checklist",
        f"{len(automated())} rows asserted in CI, {len(manual())} run by hand.",
        "",
        "Automated (green here means the wiring holds; still verify on one real "
        "machine of each kind):",
        "",
    ]
    lines += [_block(c) for c in PHASE_A if c.mode is Mode.AUTOMATED]
    lines += ["", "Manual (needs a real machine, fleet, or person at the keyboard):", ""]
    lines += [_block(c) for c in PHASE_A if c.mode is Mode.MANUAL]
    return "\n".join(lines)


def main() -> None:
    print(format_checklist())


if __name__ == "__main__":
    main()
