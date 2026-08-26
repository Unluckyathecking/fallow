# probe real hardware in the Go agent

## Status

Proposed

## Date

2026-08-26

## Goal

Make a Go-enrolled machine describe itself honestly. The daemon enrolled with
`ram_mb: 1024`, `disk_free_mb: 0`, `cpu_model: "unknown"`, no GPUs, and
heartbeated a fixed 5% CPU and 8192 MB free forever. Capability-aware placement
and the ADR 048 auto model selection read exactly those fields, so on the Go
agent — the one that runs on the pilot's Windows desks — both were choosing from
a fiction.

## Owned paths

- `go-agent/hostinfo/**` (new package)
- `go-agent/runtime/identity.go`
- `go-agent/runtime/runtime.go`
- `go-agent/runtime/site.go` (the Site Mode enrollment call site)
- `go-agent/runtime/runtime_test.go`
- `docs/adr/097-go-host-metrics.md`
- `CHANGELOG.md`

No other path belongs to this change. The Python probes are the parity
reference and are not touched; neither is the wire schema.

## Decision

One small package, `go-agent/hostinfo`, holds every hardware probe behind two
calls: `Caps(cacheDir)` for the static snapshot at enrollment, and a `Sampler`
whose `Sample()` returns the live CPU percentage, available memory and per-GPU
state for one heartbeat. It is the Go counterpart of the Python `PsutilSystemProbe` /
`NvmlGpuProbe` pair, filling the same `DeviceCaps` and heartbeat fields. No
schema changes.

No cgo anywhere: the release builds with `CGO_ENABLED=0`. Windows reads
`GlobalMemoryStatusEx`, `GetDiskFreeSpaceExW`, `GetSystemTimes`, the firmware's
`ProcessorNameString` from the registry and `RtlGetVersion`, and loads
`nvml.dll` lazily for GPU name and VRAM. Linux reads `/proc/meminfo`,
`/proc/stat`, `/proc/cpuinfo`, `/etc/os-release` and `statfs`. macOS reads
`hw.memsize`, `machdep.cpu.brand_string`, `kern.osproductversion` and `statfs`
through sysctl. Only x/sys and the standard library are used; no dependency is
added.

Two deliberate gaps, both named rather than papered over. There is no GPU probe
on Linux or macOS: NVML is the only probe the Python agent has either, and the
Linux agent is a development target, not a user platform. And macOS has no
cgo-free source for live CPU ticks or free pages — both live behind Mach calls —
so it reports a fixed 50% busy and a quarter of installed RAM as available, with
the reason in the code. Those are pessimistic constants, not measurements, and
they are not presented as measurements.

Every probe degrades on its own. A failure reports a conservative value — under
capacity, never over it — and logs its reason once, never per heartbeat. The
agent has no path that fails to start, or fails a heartbeat, because a probe
could not answer.

## Verification

Go tests cover the parsers against fixture data on any platform (`/proc/meminfo`,
`/proc/stat`, `/proc/cpuinfo`, `os-release`, the NVML name buffer) and the
CPU-delta arithmetic including a stalled and a reset counter. Live tests assert
`Caps` and `Sample` describe the machine the test runs on, and that the disk
probe still answers for a model cache directory that does not exist yet, which
is the state of every machine at enrollment. In `runtime`, `makeCaps` is
asserted to report real hardware and the first heartbeat of the end-to-end
scenario to carry sampled telemetry. The three release targets cross-compile.

## Compatibility

Wire-compatible: the same fields carry better values. The Python agent, the
coordinator and the schemas are unchanged. `agentctl`'s one-shot subcommands
keep their fixed parity-harness capabilities, which the integration suite
asserts against.

## Exclusions and honest gaps

The heartbeat's `gpus` carries index, free VRAM and utilisation, sampled from
NVML per beat on Windows. Reporting it is not optional telemetry: the coordinator
decides enrollment fit from `caps.gpus` and every later fit — `flw assign`, `GET
/agents/{id}/fit` — from these, so a GPU desk that omitted them auto-assigned
itself a GPU model at enrollment and was then judged to have no VRAM at all. The
alternative was to make the coordinator fall back to caps VRAM when a heartbeat
reports none, which would have taught it to trust a stale total in place of a
live free figure and left every non-NVML platform reporting a capacity it does
not have. Fixing the agent keeps one definition of fit. `power_w` and `temp_c`
are nullable on the wire and stay null; so do `load_avg` and `temp_cpu_c`.

NVML is loaded with `NewLazySystemDLL`, which searches System32 only. Older
driver layouts put `nvml.dll` under Program Files, where `pynvml` finds it and
this deliberately does not: a DLL search that leaves System32 is a search an
attacker can plant into, and the cost of not finding it is a machine reported as
GPU-less, which under-reports capacity rather than over-reporting it.

The Windows probes — memory, disk, registry CPU name, `RtlGetVersion`,
`GetSystemTimes` and NVML — compile in CI but are not executed by it: no Windows
runner runs the Go tests, and no NVIDIA GPU is available to any lane. They are a
pilot-day check on a real desk, not a tested path.
