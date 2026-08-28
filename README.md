# Fallow

[![CI](https://github.com/Unluckyathecking/fallow/actions/workflows/ci.yml/badge.svg)](https://github.com/Unluckyathecking/fallow/actions/workflows/ci.yml)
[![Go conformance](https://github.com/Unluckyathecking/fallow/actions/workflows/go.yml/badge.svg)](https://github.com/Unluckyathecking/fallow/actions/workflows/go.yml)
[![Install acceptance](https://github.com/Unluckyathecking/fallow/actions/workflows/install-acceptance.yml/badge.svg)](https://github.com/Unluckyathecking/fallow/actions/workflows/install-acceptance.yml)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg)](pyproject.toml)

Fallow is a pre-alpha, local-first system for evaluating whether
organisation-owned desktops and workstations can run quantised AI models and
batch workers while their users are idle. A central coordinator governs access,
placement and audit records; agents yield the machine when its user returns.

> [!WARNING]
> Fallow is for development, research and supervised evaluations, not production
> workloads. Use test machines and non-sensitive data. The full scheduling study
> has not been run, and the system has not had a production security audit.
> Grading, admissions, behaviour monitoring, profiling, biometric and other
> high-risk uses are outside the project's
> [responsible-use scope](docs/ai-act-scoping.md).

Current `main` contains substantial unreleased work beyond the `v0.3.0` tag.
The safe example below targets `main`. For a supervised deployment, pin the
exact commit you reviewed rather than a moving branch; `v0.3.0` predates the
Site Mode, model-pull and revocation work described here. Check the
[changelog](CHANGELOG.md) when choosing a ref.

## Try it without a model or GPU

The smallest example validates a model manifest. It starts no service, makes no
network request and does not need a model file:

```bash
git clone https://github.com/Unluckyathecking/fallow.git
cd fallow
uv sync --frozen --dev
uv run python examples/model_manifest.py
```

The development workspace requires Python 3.12 or 3.13,
[uv](https://docs.astral.sh/uv/) and Git.

## Choose a deployment path

| Path | Agent platform | Network and trust boundary | Start here |
| --- | --- | --- | --- |
| Local example | Any development platform in CI | No service or network | Command above |
| Direct/tailnet, Python | macOS Apple Silicon or Windows x64 | The coordinator reaches agent replicas over a trusted tailnet | [Direct quickstart](docs/quickstart.md) |
| Direct/tailnet, Go | macOS arm64 or Windows amd64 | The coordinator reaches agent replicas over a trusted tailnet | [macOS install](deploy/README.md#4-agent--macos) or [Windows install](deploy/README.md#5-agent--windows) |
| LAN Site Mode | Windows 10/11 x64 Go agents | Agents hold outbound, certificate-pinned HTTPS connections; inference stays on loopback | [Site Mode operator runbook](docs/lan-site/operator-runbook.md) |

The direct path relies on the tailnet as its trust boundary. Agent inference
servers do not provide Fallow application authentication, TLS or mTLS, so they
must not be exposed to an untrusted network. Site Mode is opt-in and does not
change the direct path. Its optional mDNS advertisement is address recovery,
not trust.

Linux is supported for the coordinator, including a systemd install. An
ordinary Linux user desktop is not a supported agent because idle detection is
deliberately unimplemented; a dedicated headless experiment host can opt into
`assume_idle` explicitly.

Fallow does not ship model weights or `llama.cpp`. Pinned fetch scripts cover
Apple Silicon macOS and Windows x64 with CUDA; other inference builds are
operator-supplied. No llama.cpp revision, GPU driver, CUDA toolkit or model
format is certified. The coordinator can stage GGUF files from a URL, Hugging
Face or a small hash-verified catalog with `flw models pull`; an air-gapped
operator can use `flw models register --file`. Agents verify model blobs before
use. The [deployment guide](deploy/README.md#0-support-matrix) records the current
runtime matrix and prerequisites.

## What is built

- An OpenAI-compatible gateway with client keys, model allowlists, admission,
  routing, streaming and audit records.
- A durable coordinator registry, job queue, lease recovery, capability-aware
  placement, model distribution, result blobs and warm-standby export.
- Chat, embedding and transcription workers, plus batch jobs and an optional
  local RAG path.
- Python and Go agents with host telemetry, idle detection, inference-process
  supervision, model caching, preemption and reclaim.
- Operator workflows for enrollment, health, model staging and assignment,
  device-token revocation and unused enrollment-token revocation.
- Build and manifest-verification tooling for a Windows Site Mode desk bundle,
  managed Windows installation, macOS launchd installation and a Linux systemd
  coordinator installer. These changes are on `main`, not in `v0.3.0`.
- A benchmark harness for workload, churn and scheduling experiments.

Peer-assisted model distribution lives in `fallow-modelmesh`. It is experimental
and disabled by default; the normal serving path distributes model blobs from
the coordinator.

## Architecture

```text
Direct / tailnet

client ──> coordinator ──> agent ──> llama.cpp or batch worker
             auth,           idle detection and preemption
             policy,
             placement

LAN Site Mode

client ──> coordinator <══ outbound pinned HTTPS ══ Windows agent ──> loopback llama.cpp
```

In both modes, the coordinator owns identity, policy, scheduling, model
manifests and audit decisions. Each capable machine runs a complete quantised
model replica or specialist worker; Fallow routes work rather than sharding a
single model across desktops. In Site Mode the coordinator does not open an
inbound connection to a desk. The desk claims work over its held relay
connection and keeps `llama-server` bound to loopback.

The repository contains a Python/uv workspace and a first-class Go agent:

| Component | Responsibility |
| --- | --- |
| `fallow-protocol` | Versioned wire models and interface contracts |
| `fallow-coordinator` | Registry, auth, queue, scheduling, model service, gateway and RAG |
| `fallow-agent` | Python agent for the direct deployment path |
| `go-agent` | Direct agent for macOS/Windows and the Windows Site Mode agent |
| `fallow-cli` | The `flw` operator CLI and admin client |
| `fallow-bench` | Workload, churn, experiment and analysis tools |
| `fallow-modelmesh` | Experimental peer-assisted model distribution primitives |

Protocol schemas are generated into [`schemas/`](schemas/) and checked against
both implementations in CI. Architecture decisions are recorded in the
[ADR index](docs/adr/README.md).

## Evidence and limits

The preemption spike measured end-to-end p99 yield latency of 103.1 ms on the
development Mac and 116.3 ms on the Windows/RTX machine under full CPU load,
against the current 300 ms engineering target. The same two-machine exercise
demonstrated model distribution, gateway streaming, user-return preemption and
machine-loss failover. Raw measurements and conditions are in the
[spike results](experiments/spikes/RESULTS.md).

That is limited engineering evidence from two machines, not proof of production
behaviour. The planned 18-hour scheduling experiment remains unrun; its protocol
is documented separately in the [experiment plan](docs/experiment.md).

CI currently checks the Python workspace on Ubuntu, macOS and Windows with
Python 3.12 and 3.13, tests the Go agent and Python/Go protocol parity, and
exercises the Windows and macOS installers on hosted runners. It does not prove
behaviour under a real site's LAN policy, EDR, SmartScreen, interactive logon or
hardware mix. Released bundles are not code-signed. There is no claim of
multi-tenant isolation, high availability or an independent security review.

## Development and contributing

For a first local check:

```bash
uv sync --frozen --dev
uv run pytest
(cd go-agent && go test ./...)
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) for the complete quality gate and change
process. Development has included coding agents; acceptance rests on maintainer
review, tests, CI and recorded experiments. Notable changes are kept in the
[changelog](CHANGELOG.md).

## Security and support

Do not report vulnerabilities in public issues. Use a
[private GitHub security advisory](https://github.com/Unluckyathecking/fallow/security/advisories/new).
For help requests, see [SUPPORT.md](SUPPORT.md).

## License

Fallow is licensed under the
[GNU Affero General Public License v3.0 or later](LICENSE). Contributions are
accepted under the same license; see the
[contribution terms](CONTRIBUTING.md#licensing-and-developer-certificate-of-origin).
