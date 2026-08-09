# write per-device Site Mode join files

## Status

Proposed

## Date

2026-08-09

## Related

#109, #110, #113

## Goal

Give the operator one command that requests distinct join files and writes them without exposing their tokens.

## Owned paths

- `packages/fallow-cli/src/fallow_cli/site/**`
- `packages/fallow-cli/src/fallow_cli/main.py`
- `packages/fallow-cli/src/fallow_cli/client.py`
- `packages/fallow-cli/src/fallow_cli/models.py`
- `packages/fallow-cli/src/fallow_cli/README.md`
- `packages/fallow-cli/tests/test_cli_site.py`
- `docs/adr/083-lan-site-cli.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

`flw site join-bundles --count 4 --output DIR` calls `POST /v1/admin/site/join-bundles` with the existing admin key. The command validates every bundle against v1 before writing `desk-01.fallow-join` and so on with atomic replace and owner-only permissions. Existing files cause a failure unless `--force` is given.

Human output lists paths, site ID, coordinator origins and a short pin prefix. `--json` returns the same non-secret metadata. Neither mode prints tokens or full bundle contents. This command uses a direct no-proxy client for the site endpoint; existing CLI commands retain their current proxy behaviour.

## Verification

Tests cover the exact request, count bounds, malformed server data, partial-write cleanup, permissions, overwrite refusal, admin rejection and stdout/stderr redaction. A generated fixture must parse in the Go site-client tests.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No certificate generation, coordinator implementation, installer or discovery. The operator supplies the coordinator admin URL and key as today.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
