# ADR 044: Linux idle detection via XScreenSaver with a headless fallback

Status: accepted · Date: 2026-07-16

## Problem

Linux idle detection (module A1) was an honest stub that raised on use, so
Linux desktops, servers, and VMs could not run as agents. Unlike macOS and
Windows there is no single API that covers every Linux host: a desktop under
X11, a headless server or VM with no display, and a Wayland session each report
input differently, and some have nothing to report at all.

## Decision

Cover the three real cases explicitly rather than assume a desktop.

- **X11 desktop.** Read the `idle` field of `XScreenSaverInfo` through the
  MIT-SCREEN-SAVER extension (libXss), called via `ctypes` against libX11 and
  libXss. `idle` is milliseconds since the last input; the reader divides by
  1000. The display connection and info struct are opened once and reused, so
  each ~10 Hz poll is a single X round-trip, not a fresh connect. This is the
  primary path and adds no pip dependency and no external binary.
- **Headless server / VM.** With no display there is no input to measure, and a
  dedicated compute node is effectively always available, so the detector
  reports the largest finite float (always-idle). The gate is the `DISPLAY`
  environment variable, checked at construction: a real desktop always has it
  set, so it can never silently fall to always-idle by mistake.
- **Wayland / no libXss.** If a display is present but the XScreenSaver read is
  unavailable — libXss missing, no display reachable, or the extension absent —
  fall back to the headless value and log once that idle-based preemption is
  degraded on this host.

The reader is chosen once at construction. Construction never raises on a
no-display host, so the factory can return the detector unconditionally on
Linux.

### Why libXss + headless fallback over D-Bus/logind

`org.freedesktop.login1` exposes an `IdleHint`, but it is coarse (a boolean
tied to session idle policy, not seconds-since-input) and pulling it in means a
D-Bus client dependency and a message round-trip on the hot path. XScreenSaver
gives an exact millisecond counter through a two-call ctypes read with no new
dependency, which fits the microsecond, standard-library-only contract the
other detectors hold to. A logind/D-Bus path remains a documented future option
for Wayland-only hosts; it is deliberately not added here.

## Test

`test_idle_linux.py` drives every seam without touching the OS: a fake query
function and info struct assert the millisecond-to-second conversion and the
query arguments; the query-failure path asserts a clear `OSError`; the resolver
tests assert headless selection without `DISPLAY`, the real reader when it is
present, and the degraded fallback when the X11 build raises. Construction on a
host with `DISPLAY` unset is asserted not to raise and to report always-idle.
The real XScreenSaver read runs only behind a skip gated on `$DISPLAY`, so CI
(which has no display) skips it rather than faking a pass.
