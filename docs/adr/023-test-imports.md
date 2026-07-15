# ADR 023: Test helpers live in named modules, never in conftest

Status: accepted · Date: 2026-07-15

## Problem

22 test files imported shared helpers with `from conftest import X`. pytest
imports every `conftest.py` under a unique *internal* module name (keyed by
path), but a test module's own `import conftest` is an ordinary import resolved
against `sys.path`. Under the default `prepend` import mode, pytest inserts each
test file's directory at `sys.path[0]` as it collects, so the `conftest` a test
sees is whichever directory currently sits first on the path.

The full suite passed only by collection-order luck. Running a **subset that
mixes trees**, e.g.
`pytest packages/fallow-coordinator/tests/gateway tests/integration`, let the
wrong `conftest` win and raised `ImportError` (e.g. `cannot import name
'GatewayHarness' from 'conftest'`). This made it impossible to reliably run a
focused slice of the suite.

## Rule

**Never import from `conftest`. `conftest.py` is fixtures-only.**

- Every non-fixture export (helper functions, fakes, dataclasses, constants)
  lives in a directory-local, **globally uniquely-named** helpers module:
  `gateway_helpers.py`, `app_helpers.py`, `integration_helpers.py`,
  `heartbeat_helpers.py`, `modelcache_helpers.py`, `scheduler_helpers.py`,
  `queue_helpers.py`, etc.
- `conftest.py` contains only pytest fixtures plus imports *from* its helpers
  module.
- Test modules import shared helpers from the named helpers module, never from
  `conftest`.

Helper module basenames must be unique across the whole repo: all test
directories share one pytest `sys.path` universe, so two `foo_helpers.py` files
in different trees would collide the same way `conftest` did. The convention is
`<dirname>_helpers.py`.

## Fix

Moved all non-fixture symbols out of each `conftest.py` into its sibling
helpers module (creating the module where absent), reduced each `conftest.py`
to fixtures + helper-module imports, and rewrote every `from conftest import …`
to import from the helpers module. Verified by running the previously-broken
mixed-tree subset and additional cross-tree slices, all green.
