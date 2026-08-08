# add pinned HTTPS site bootstrap

## Status

Proposed

## Date

2026-08-09

## Related

#109

## Goal

Add the opt-in coordinator identity and HTTPS listener, plus a router that can mint strict v1 join files through injected enrollment-token storage.

## Owned paths

- `packages/fallow-coordinator/src/fallow_coordinator/site/**`
- `packages/fallow-coordinator/tests/site/**`
- `packages/fallow-coordinator/src/fallow_coordinator/app/config.py`
- `packages/fallow-coordinator/src/fallow_coordinator/__main__.py`
- `packages/fallow-coordinator/pyproject.toml`
- `uv.lock`
- `docs/adr/079-lan-site-coordinator.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

Input config adds `[site] enabled`, `site_id`, `public_urls`, `tls_certfile`, `tls_keyfile` and optional `mdns_service`. Site Mode requires an exact non-wildcard bind, HTTPS public origins and a matching certificate/key pair. Disabled or absent site config preserves today's HTTP defaults.

`build_site_admin_router(settings, create_site_token)` exposes `POST /v1/admin/site/join-bundles` with admin bearer auth. Request `{"count":4}` accepts 1 through 16. Response `201` is `{"bundles":[JoinBundleV1...]}`. Each bundle has its own existing one-use token. The router hashes the leaf certificate `RawSubjectPublicKeyInfo` with SHA-256 and returns standard-base64 pins. It never returns the key or logs tokens. The router is not mounted until the coordinator integration PR.

`python -m fallow_coordinator serve` passes the configured certificate and key to uvicorn. Site Mode has no cleartext companion listener.

## Verification

Config tests prove partial TLS, HTTP origins and wildcard binds fail before startup. A local HTTPS test checks the presented SPKI against the bundle. Router tests cover admin auth, count bounds, distinct tokens, strict schema, redaction and a fake token callback. Run the full Python quality gate and `uv lock --check`.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No registry migration, app factory wiring, relay, mDNS, CLI or Go changes. Do not edit `docs/adr/README.md` or `CHANGELOG.md`.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
