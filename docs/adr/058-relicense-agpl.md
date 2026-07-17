# ADR 058: Relicense from Apache-2.0 to AGPL-3.0-or-later

Status: accepted · Date: 2026-07-17

## Context

Fallow shipped under Apache-2.0. That was a reasonable default for an early
open-source project, but it is a permissive licence: anyone can take the code,
build a closed product on top of it, run it as a hosted service, and give
nothing back. For a project whose point is to be a public-interest commons — a
shared compute layer that organisations run and improve together — that is the
wrong incentive. Apache-2.0 lets the commons be enclosed.

Fallow is also, by design, a networked service. The coordinator and agents are
meant to be run for users over a network, often without those users ever
receiving a binary. Under GPL-3.0 that hosted-service case is a loophole: a
provider can modify the code, offer it as a service, and never trigger the
obligation to share source, because they never "distribute" anything. The GNU
Affero clause exists precisely to close that gap.

## Decision

Relicense the whole project to `AGPL-3.0-or-later`.

- **Full licence text.** `LICENSE` is replaced with the verbatim official text
  of the GNU Affero General Public License v3.0 from the Free Software
  Foundation.
- **Package metadata.** The root `pyproject.toml` and every package
  `pyproject.toml` set `license = "AGPL-3.0-or-later"` (SPDX), and each OSI
  classifier becomes
  `License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)`.
- **README.** The licence section and badge point to AGPL-3.0.
- **Copyleft is the point.** AGPL keeps derivatives open: anyone who builds on
  Fallow and conveys it, or runs a modified version as a network service, must
  offer users the corresponding source under the same terms. That prevents
  proprietary capture of the commons while leaving the code fully open to use and
  modify.
- **Network-use clause fits the deployment model.** Fallow is normally reached
  over a network rather than installed by its users, so the Affero section 13
  obligation — offer source to remote users — is what actually keeps a hosted
  fork honest. Plain GPL would not.
- **All contributions henceforth are AGPL.** From this change on, contributions
  are made under AGPL-3.0-or-later, consistent with the inbound-equals-outbound
  terms in `CONTRIBUTING.md`.

This is a deliberate, one-directional change away from Apache-2.0, made while
the project is young and the contributor set is small enough that the switch is
clean.

## Consequences

- **No code or behaviour changes.** This is licence text, package metadata, and
  README only. Tests and runtime are untouched.
- **Downstream obligations are stronger.** Anyone integrating Fallow into a
  larger work, or hosting a modified version, now inherits AGPL obligations. That
  is the intended effect: it protects the commons, and it does rule out closed
  or permissively-relicensed derivatives.
- **Dependency compatibility.** AGPL-3.0 can consume Apache-2.0 and other
  permissive dependencies. It is more restrictive on the outbound side, which is
  the tradeoff being chosen on purpose.
- **Stray references to reconcile.** A few non-blocking files still name
  Apache-2.0 (`CITATION.cff`, `CONTRIBUTING.md`, `OPEN_SOURCE_CHECKLIST.md`,
  `examples/model_manifest.py`). They are outside this change's scope and are
  left for a follow-up so this PR stays surgical.
