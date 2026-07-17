# ADR 063: Machine-wide broker vs per-user agent on shared PCs

**Status:** Accepted

**Date:** 2026-07-17

## Context

The agent installs per-user. On macOS it is a LaunchAgent under the installing
user in `gui/$UID`; on Windows it is an at-logon Scheduled Task with
`LogonType=InteractiveToken`. Both run inside the logged-in desktop session, and
they have to. Idle detection reads the active session's input timer (Quartz
`CGEventSourceSecondsSinceLastEventType` on macOS, `GetLastInputInfo` on
Windows), and those APIs only return real values inside an interactive GUI
session. A macOS LaunchDaemon or a Windows Service runs in session 0 with no
window server or input desk, always reads "idle", and would never yield the
machine back to the person using it. ADR 017 records this constraint, and it is
not going away: the agent has to live where the user's session lives.

State follows the same shape. After enrollment the agent writes its identity to
`~/.fallow/agent-state.json` (0600) and keeps its model cache, event log, and
results under `~/.fallow/`. All of that sits in the installing user's profile.

This is fine on a single-owner machine, which is what the deploy scripts assume.
School PCs break the assumption. A lab machine is commonly shared by a whole
class: many student accounts, one box, roaming or local profiles, users logging
in and out through the day. The per-user model does not fit that, in three ways.

- **It only runs under the one account that installed it.** Install under a
  student login and the agent is present for that student and absent for the
  next thirty. Whoever is actually sitting at the machine is usually not the
  account that has the agent, so the box contributes nothing.
- **Multiple installs collide.** If several profiles each install and enroll, a
  single physical machine ends up running several agents at once, during fast
  user switching or wherever more than one session is live. They contend for
  the same fixed local port range and each keep their own copy of the model
  cache, so multi-GB blobs get duplicated per profile and burn disk that a lab
  image does not have to spare.
- **Device identity is ambiguous.** Enrollment issues an identity to an agent,
  not to a machine. Per-user, "one machine" and "one agent" stop being the same
  thing: the coordinator sees accounts, not hardware, so capacity accounting,
  assignment, and takedown are all reasoning about the wrong unit.

## Decision

For the pilot, run on machines set up with a single agreed pilot account, and do
not attempt general multi-user lab deployment yet.

Concretely: the school nominates one account per pilot machine, the agent is
installed and enrolled once under that account, and idle time is harvested while
that account is the logged-in session. This keeps every property the per-user
model already gives us — one agent per machine, one identity, one cache, one
port range, idle detection reading the session it runs in — with no new code.
Whether the pilot account is a dedicated login or an existing shared-class login
is the school's call; the requirement is one agent install per machine, not one
per student.

We are not shipping a multi-user lab installer, a machine-wide service, or any
cross-session broker in this round. On a machine where students log into their
own accounts and the pilot account is not the active session, that machine
simply does not contribute for now. That is an accepted gap, not a bug to code
around at pilot scale.

## Longer-term design

General lab deployment wants the machine, not a user, to own the agent. The
shape that fits is a split: a machine-wide broker plus a small per-session idle
helper.

**Machine-wide broker.** A system-context service (LaunchDaemon on macOS, a
Windows Service) installed once per machine. It owns the parts that are properly
per-machine: the device identity and enrollment, the model cache, and the
inference processes and their local ports. One identity per box, one cache, one
owner of the port range. Because it holds the coordinator uplink and the
replicas, model work does not care which human is logged in, and duplication and
port contention go away by construction.

**Per-session idle helper.** A tiny per-user agent, installed the way the
current agent is (LaunchAgent / at-logon task), running in each interactive
session. It does one job: read that session's input timer and report activity to
the broker over a local channel. It runs no models and holds no identity or
cache. Keeping it minimal is what makes it safe to have one per logged-in user.

The broker treats the machine as busy when *any* live session reports the user
as active, and only runs work when every session is idle. That is the honest
reading of "is someone using this computer" on a multi-seat box, and it is the
piece the current single-session model cannot express.

The tradeoffs are real and are why this is a design, not this PR:

- A system service plus a per-user helper plus the local channel between them is
  more moving parts, more failure modes, and more to install, supervise, and
  uninstall than one at-logon task. The broker restarting must not orphan
  replicas or lose the "any session active" view; the helper dying must fail
  toward "assume active", never toward running work over a present user.
- The system service reintroduces exactly the session-0 problem ADR 017 avoided.
  It works only because idle detection has been pushed out to the per-session
  helpers, which do live in the GUI sessions; the broker itself never calls the
  idle APIs. That division has to hold.
- A machine-wide install is a bigger security-review ask — a privileged service
  binding local ports and spawning `llama-server`, on top of the
  Defender/SmartScreen/EDR allowlisting the per-user install already needs.

## Why deferred

The pilot is three to five machines. Standing up a privileged broker, a
per-session helper, and a local IPC contract to serve that is a large increase
in surface for machines we can instead just configure with one pilot account.
The single-account approach needs no new code, keeps the identity and accounting
model we already have, and lets the pilot get at the questions it exists to
answer: does idle harvesting hold up, is preemption clean, do the numbers work.
None of that needs multi-user distributed state first.

The cost is the machines that only ever run under per-student logins, which
contribute nothing under the single-account setup. At three to five machines
that is a rounding error. It stops being one at lab scale, where a full cart of
shared PCs is exactly the machine the broker is for, so this is revisited when a
deployment's value turns on covering those machines. Until
then the per-user install and a nominated pilot account are the right amount of
machinery.
