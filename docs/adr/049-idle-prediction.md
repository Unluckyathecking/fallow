# ADR 049: Idle-time prediction on top of the idle detector

Status: accepted · Date: 2026-07-16

## Problem

The idle detectors (ADR 001, 040, 044) report one number: seconds since the
last input, right now. That is the present. Scheduling a model onto an idle
machine also wants the near future. Loading a multi-GB model onto a laptop that
its owner is about to reopen wastes the pull, the VRAM, and the user's goodwill;
winding a replica down only after the user is already typing is a beat too late.
Both need an estimate of how much longer a machine will stay idle, and how much
to trust that estimate.

This is a first cut. The goal is to measure the signal and get it to the
coordinator, not to change any scheduling decision yet. Measurement before
policy: until the predicted numbers are visible next to real outcomes, tuning a
policy on them would be guesswork.

## Decision

Add an `IdlePredictor` as a new module that consumes the `IdleDetector` seam.
It does not touch the detectors and it is off by default.

### The model

Keep it simple and explainable. No ML dependency, no training step, nothing a
reader cannot trace by hand.

- The predictor watches the detector's reading over successive samples. When the
  reading drops, the user touched the machine, so the idle window that just
  ended is folded into an exponential moving average of typical window length.
  History is bounded to a fixed number of recent windows.
- For the window in progress, the remaining estimate is
  `min(elapsed, typical)`. A machine that has already stayed idle a long time is
  projected to stay idle longer — the inspection-paradox intuition that long
  gaps tend to keep going — capped at the typical length so one unusually long
  absence cannot make the estimate run away. It falls back to zero the moment
  input resets the window, which is exactly when the machine is least available.
- Confidence rises with the number of windows observed and falls once the
  current window runs past the typical (EWMA) window length, since past that
  point the estimate is extrapolating rather than interpolating.

### Why the detector is the only clock

The predictor introduces no wall clock and no timer. The detector's reading is
itself the elapsed-idle signal, so a second time source would only drift against
it and add a seam to keep in sync. Sampling is driven by the heartbeat that
already runs on its own cadence. This keeps the module deterministic: tests
script the readings through `FakeIdleDetector` and assert on the output, with no
sleeps and no real time.

### On the wire, and off by default

A machine-local flag (`idle_prediction_enabled`, default false) turns it on. The
two fields, `predicted_idle_remaining_s` and `predicted_idle_confidence`, are
always present on the heartbeat; when the flag is off nothing is computed and they
ride as null, exactly like `load_avg` and `temp_cpu_c`. They are never omitted:
`FallowModel` forbids unknown fields and fails loud on drift, so every field
serialises the same way whether set or null. When the flag is on, the predictor
fills them in and the coordinator records them on the agent row and exposes them
on `AgentSnapshot`. Nothing consumes them for scheduling in this change; that is a
deliberate follow-up.

## Test

`test_idle_predictor.py` drives the model through a fake detector: the remaining
estimate rises as an idle window extends and drops to zero once input resets it;
it stays capped at the typical window length; confidence grows as windows
accumulate, decays when the current window runs past the typical length, and never leaves
the unit interval; and the history stays bounded. `test_heartbeat_loop.py`
asserts the fields are absent by default and carry the predictor's output when a
predictor is wired in. On the coordinator, `test_registry_snapshots.py` asserts a
heartbeat's prediction is recorded and surfaced on the snapshot, and is null
until one is reported. The regenerated schemas and Go types keep the Python and
Go wire views in step.
