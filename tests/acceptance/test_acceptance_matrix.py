"""The matrix stays honest: every row is either asserted here or has a command.

Guards the single source of truth in acceptance_matrix.py. An automated row
must point at a test file that exists; a manual row must carry a command and an
expected result. This is what stops a check from silently going missing.
"""

from __future__ import annotations

from acceptance_helpers import REPO_ROOT
from acceptance_matrix import PHASE_A, Mode, automated, manual
from acceptance_runner import format_checklist

EXPECTED_IDS = frozenset(
    {
        "clean-install",
        "reboot-persistence",
        "user-return-preemption",
        "agent-killed-reroute",
        "coordinator-restart-recovery",
        "network-removed-no-storm",
        "model-corrupt-rejection",
        "active-user-suspend",
        "uninstall-completeness",
        "log-hygiene",
    }
)


def test_matrix_covers_every_phase_a_row() -> None:
    ids = [c.id for c in PHASE_A]

    assert set(ids) == EXPECTED_IDS
    assert len(ids) == len(set(ids)), "duplicate check id"


def test_split_is_five_automated_five_manual() -> None:
    assert len(automated()) == 5
    assert len(manual()) == 5


def test_automated_rows_have_an_existing_test() -> None:
    for check in automated():
        assert check.evidence, f"{check.id} is automated but names no test"
        assert (REPO_ROOT / check.evidence).is_file(), f"missing evidence for {check.id}"


def test_manual_rows_carry_a_command_and_expected_result() -> None:
    for check in manual():
        assert check.mode is Mode.MANUAL
        assert check.command.strip(), f"{check.id} has no command"
        assert check.expected.strip(), f"{check.id} has no expected result"
        assert not check.evidence, f"{check.id} is manual but names a test"


def test_runner_lists_every_check() -> None:
    text = format_checklist()

    for check in PHASE_A:
        assert check.id in text
