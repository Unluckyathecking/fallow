"""The Phase-A pilot acceptance matrix as data.

One row per check from the school-pilot readiness brief (docs/school-pilot.md
section 8, plus the uninstall check from section 6). This module is the single
source of truth: the runner prints it for IT, and test_acceptance_matrix.py
asserts every row is either backed by a test in this suite or carries an exact
command a human runs on a real machine.

"automated" means CI asserts the behaviour with fakes and dry-run renders, no
real device, model, network, or GPU. "manual" means the check needs a running
fleet or a person at the keyboard, so the row carries the exact command to run
and what a correct pilot does.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Mode(StrEnum):
    AUTOMATED = "automated"
    MANUAL = "manual"


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    doc_ref: str  # section of docs/school-pilot.md the row traces to
    mode: Mode
    command: str  # how to run it: a pytest command, or the on-device command
    expected: str
    evidence: str = ""  # automated rows: repo-relative path of the asserting test


PHASE_A: tuple[Check, ...] = (
    Check(
        id="clean-install",
        title="Clean install, no manual code edits",
        doc_ref="8.1",
        mode=Mode.AUTOMATED,
        command="uv run pytest tests/acceptance/test_acceptance_install.py",
        expected=(
            "The installer renders its launch item from templates and the shipped "
            "example config. It needs no source edit, and a dry run touches nothing "
            "on disk."
        ),
        evidence="tests/acceptance/test_acceptance_install.py",
    ),
    Check(
        id="reboot-persistence",
        title="Restarts at login, not at boot",
        doc_ref="8.2",
        mode=Mode.AUTOMATED,
        command="uv run pytest tests/acceptance/test_acceptance_install.py",
        expected=(
            "The macOS LaunchAgent sets RunAtLoad and KeepAlive; the Windows task "
            "triggers at logon with restart-on-failure and no boot trigger. The agent "
            "comes back on the next login and relaunches after a crash."
        ),
        evidence="tests/acceptance/test_acceptance_install.py",
    ),
    Check(
        id="user-return-preemption",
        title="User return yields compute",
        doc_ref="8.3",
        mode=Mode.MANUAL,
        command=(
            "On a serving pilot machine, move the mouse or type. For an immediate "
            "hand-back run: python -m fallow_agent reclaim ; then python -m "
            "fallow_agent release to resume idle serving."
        ),
        expected="Serving yields promptly and the replica suspends (target p99 under 300 ms).",
    ),
    Check(
        id="agent-killed-reroute",
        title="Killed agent reroutes or fails clean",
        doc_ref="8.4",
        mode=Mode.MANUAL,
        command=(
            "Kill the agent process on one machine while another serves the same "
            "model, then watch: flw agents list"
        ),
        expected=(
            "The coordinator marks it suspect (~15 s) then offline (~45 s), interactive "
            "traffic reroutes to the other replica, and leases requeue."
        ),
    ),
    Check(
        id="coordinator-restart-recovery",
        title="Coordinator restart recovers",
        doc_ref="8.5",
        mode=Mode.MANUAL,
        command="Restart the coordinator process, then: flw agents list",
        expected=(
            "Agents re-register on their next heartbeat and reappear, in-flight batch "
            "leases requeue, and persisted state survives with no data loss."
        ),
    ),
    Check(
        id="network-removed-no-storm",
        title="Network removal, no retry storm",
        doc_ref="8.6",
        mode=Mode.MANUAL,
        command=(
            "Drop the machine off the tailnet then rejoin: tailscale down ; tailscale "
            "up. On macOS watch ~/.fallow/logs/agent.err.log; on Windows watch flw "
            "agents list for the drop and return."
        ),
        expected=(
            "The agent backs off and reconnects at a steady cadence, with no tight "
            "reconnect loop flooding the log."
        ),
    ),
    Check(
        id="model-corrupt-rejection",
        title="Corrupt model is rejected by hash",
        doc_ref="8.7",
        mode=Mode.AUTOMATED,
        command="uv run pytest packages/fallow-agent/tests/modelcache/test_modelcache_verify.py",
        expected=(
            "The agent model cache checks a downloaded blob's SHA256 and byte size "
            "against the manifest and refuses to serve on any mismatch. No replica "
            "starts on the bad file."
        ),
        evidence="packages/fallow-agent/tests/modelcache/test_modelcache_verify.py",
    ),
    Check(
        id="active-user-suspend",
        title="Active user suspends new work",
        doc_ref="8.8",
        mode=Mode.MANUAL,
        command=(
            "Keep the machine actively in use, then assign it a new model with an "
            "admin PUT /v1/admin/assignments."
        ),
        expected=(
            "No new replica starts while the user is active; the reconcile defers until "
            "the machine goes idle."
        ),
    ),
    Check(
        id="uninstall-completeness",
        title="Uninstall removes launch item and state",
        doc_ref="6",
        mode=Mode.AUTOMATED,
        command="uv run pytest tests/acceptance/test_acceptance_uninstall.py",
        expected=(
            "Uninstall removes the launch item and, with the purge flag, deletes "
            "~/.fallow. Freeing orphaned replica ports and processes is best-effort and "
            "confirmed on a real serving machine."
        ),
        evidence="tests/acceptance/test_acceptance_uninstall.py",
    ),
    Check(
        id="log-hygiene",
        title="Logs carry metadata only",
        doc_ref="8.9",
        mode=Mode.AUTOMATED,
        command="uv run pytest tests/acceptance/test_acceptance_log_hygiene.py",
        expected=(
            "The gateway request log records per-request metadata only: key name, "
            "model, agent, timestamps, status, and a prompt-length count. No prompt "
            "text, document or response content, secrets, or end-user identity."
        ),
        evidence="tests/acceptance/test_acceptance_log_hygiene.py",
    ),
)


def automated() -> tuple[Check, ...]:
    return tuple(c for c in PHASE_A if c.mode is Mode.AUTOMATED)


def manual() -> tuple[Check, ...]:
    return tuple(c for c in PHASE_A if c.mode is Mode.MANUAL)
