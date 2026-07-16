# ADR 040: macOS idle read binds CoreGraphics via ctypes, not a pyobjc lazy attribute

Status: accepted · Date: 2026-07-16

## Problem

The darwin idle detector (ADR 001, module A1) read seconds-since-input by
importing the pyobjc `Quartz` module and pulling
`CGEventSourceSecondsSinceLastEventType` off it with `getattr`. On current
pyobjc builds that name is not exposed as a module attribute, so the lookup
raised at runtime (`KeyError` from the lazy-constant machinery, issue #34). The
preempt poll thread hit it every tick and logged an overrun; the darwin idle
read was effectively broken, and the whole path depended on pyobjc being both
installed and exposing that particular symbol.

## Decision

Bind the function through `ctypes` against the CoreGraphics framework instead
of through pyobjc. `ctypes.util.find_library("CoreGraphics")` locates the
framework, `ctypes.CDLL` loads it, and the symbol is resolved directly off the
C export, which is always present, with an explicit signature
(`restype = c_double`, two `c_uint32` arguments). The call still takes
`kCGEventSourceStateHIDSystemState` and `kCGAnyInputEventType`, whose integer
values (`1` and `0xFFFFFFFF`) are named constants.

Consequences:

- No pyobjc dependency. The idle read now needs only the standard library.
- The framework is loaded lazily and inside the `sys.platform == "darwin"`
  branch, so the module still imports cleanly on other platforms.
- The load is memoised (`lru_cache`), since the poll thread calls this at
  ~10 Hz and `find_library` is not free.
- A missing symbol surfaces as a clear `OSError`, not a raw ctypes error.

## Test

`test_idle_darwin.py` injects a fake CoreGraphics library at the resolver seam,
so the missing-symbol case (the issue #34 regression) is caught on any host:
one test asserts the C signature and call arguments are bound correctly, another
asserts that a library without the symbol raises a clear `OSError`. No test
touches pyobjc or the real framework.
