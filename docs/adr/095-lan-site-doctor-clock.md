# report clock skew in Site Mode doctor

## Status

Proposed

## Date

2026-08-09

## Related

#115, #118, #119

## Goal

Surface a drifted PC clock as a named diagnostic before it presents as an opaque pinned-TLS failure. School machines are not guaranteed NTP sync, and certificate validation is the first thing a bad clock breaks.

## Owned paths

- `go-agent/cmd/agentctl/main.go`
- `go-agent/runtime/doctor.go`
- `go-agent/runtime/doctor_test.go`
- `deploy/windows/doctor.ps1`
- `docs/adr/095-lan-site-doctor-clock.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

`agentctl doctor` gains a clock check when a Site profile exists: it reads the coordinator's `Date` header over the existing pinned client and reports the signed offset between local time and coordinator time. Offsets over 120 seconds are flagged; smaller offsets are reported without a flag. When the coordinator is unreachable or the pin fails, the check reports that reason distinctly instead of guessing at skew.

The check stays read-only and sends no token; the `Date` header is available on the unauthenticated response the pinned client already receives before authorization. `doctor.ps1` renders the new field alongside its existing checks and adds no clock logic of its own.

## Verification

Go tests cover skew calculation, the flag threshold, an unreachable coordinator and a pin failure, using an injected clock and test server. A Pester case asserts `doctor.ps1` passes the field through unmodified.

## Compatibility

Diagnostic only. Doctor still never enrolls, claims work or mutates state. Behaviour without a Site profile is unchanged.
