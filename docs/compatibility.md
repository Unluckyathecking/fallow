# Compatibility policy

This document separates environments continuously tested by the project from environments that
may work but are not currently supported.

## Development and library compatibility

| Component | Supported | CI coverage |
| --- | --- | --- |
| Python | 3.12 and 3.13 | Both versions |
| Linux | Current GitHub-hosted Ubuntu image, x86-64 | Python 3.12 and 3.13 |
| macOS | Current GitHub-hosted macOS image, Apple Silicon or x86-64 | Python 3.12 and 3.13 |
| Windows | Current GitHub-hosted Windows image, x86-64 | Python 3.12 and 3.13 |
| Package manager | Current stable uv compatible with `uv.lock` | Pinned CI action |

Python 3.14 and alternative interpreters are not yet supported. Linux distributions and CPU
architectures outside the CI matrix may work but are community-tested. The pure
`fallow-protocol` package is intended to be portable across all supported Python platforms.

## Runtime dependencies

Fallow does not bundle model weights or an inference engine. The design currently targets a
`llama.cpp` server exposing an OpenAI-compatible local HTTP API. No llama.cpp revision, GPU
driver, CUDA toolkit or model format is certified yet because runnable coordinator and agent
composition is incomplete.

Platform capabilities differ:

- macOS idle detection uses Quartz and requires the permissions imposed by the host OS.
- Windows and Linux use their platform-native idle detection paths.
- NVIDIA telemetry requires a compatible NVIDIA driver; CPU-only and Apple Silicon paths must
  not require it at runtime when no NVIDIA GPU is present.
- Worker inference servers bind to the agent's tailnet IP in production and to loopback only for
  single-machine development ([ADR 052](adr/052-replica-bind-address-safety.md)). They are
  unauthenticated, so the tailnet (Tailscale or WireGuard) provides transport confidentiality;
  there is no application-layer TLS yet.

## LAN Site Mode

LAN Site Mode ([ADR 078](adr/078-lan-site-mode.md)) is an opt-in second deployment shape for a
site with no tailnet and no internet. It is additive: a coordinator without `[site].enabled` and
an agent without `site_join_bundle` behave exactly as documented above, and the existing explicit
URL and Tailscale paths are unchanged.

| Aspect | Legacy (tailnet) | LAN Site Mode |
| --- | --- | --- |
| Agent | Python or Go | Go only, Windows for the pilot |
| Transport | Tailnet, cleartext HTTP inside it | Pinned HTTPS to one coordinator, outbound only |
| Trust anchor | The tailnet | SHA-256 of the coordinator certificate's SubjectPublicKeyInfo |
| Replica bind | The agent's tailnet IP | `127.0.0.1` only, enforced by config and by `doctor` |
| Coordinator bind | Tailnet address | An exact non-wildcard LAN address; wildcards are refused |
| Addressing | MagicDNS or tailnet IP | Static address (DHCP reservation or internal DNS); mDNS is optional recovery |

The pilot is proven on Windows 10 and 11 in the logged-in user's session. macOS and Linux agents
have no Site Mode. Operator procedure and its honest gaps are in the
[Site Mode operator runbook](lan-site/operator-runbook.md).

## Compatibility changes

CI must pass on every supported Python/OS combination before it may be removed from this table.
Dropping a supported platform or Python version is a user-visible breaking change and must be
announced in the changelog. See [api-stability.md](api-stability.md) for protocol and API rules.
