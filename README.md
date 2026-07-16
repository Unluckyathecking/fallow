# Fallow

[![CI](https://github.com/Unluckyathecking/fallow/actions/workflows/ci.yml/badge.svg)](https://github.com/Unluckyathecking/fallow/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg)](docs/compatibility.md)

Fallow is an experimental AI compute layer that aims to turn spare capacity on an
organisation's desktops and workstations into a centrally governed inference and batch
processing fabric—without disrupting the people using those machines.

## Project status

**Pre-alpha: suitable for development and research only.** All core modules exist with
tests, including the composition entrypoints: `python -m fallow_coordinator serve` runs the
coordinator and `python -m fallow_agent run` runs the per-machine agent, with an
end-to-end integration suite covering lifecycle, batch jobs, churn recovery, preemption
and gateway streaming. The canonical scheduling plan and warning-free smoke acceptance
are implemented; the full 18-hour hardware study has not been run. It has not had a
production security audit. Follow the
[roadmap](ROADMAP.md) and [changelog](CHANGELOG.md) for progress.

Please do not use Fallow for production workloads or high-risk decisions. In particular, the
project does not support grading, admissions, behaviour monitoring, profiling, biometric or
other high-risk uses. See the [responsible-use scope](docs/ai-act-scoping.md).

## Why Fallow?

- **Replication, not sharding.** Each capable machine runs a complete quantised model or a
  specialist worker; requests route to an available replica.
- **Fast preemption.** Work yields when a person returns to a machine. The current engineering
  target is p99 under 300 ms; measured spike results are in
  [`experiments/spikes/RESULTS.md`](experiments/spikes/RESULTS.md).
- **Central governance, distributed execution.** A coordinator owns identity, policy,
  scheduling and audit decisions while workers execute jobs.
- **Local-first design.** Deployments are intended to keep prompts, documents and weights on
  infrastructure controlled by the operator.

## Architecture

```text
clients ──> OpenAI-compatible gateway ──> coordinator
                                           │
                       ┌───────────────────┼───────────────────┐
                       v                   v                   v
                    agent               agent               agent
                  llama.cpp          embeddings          transcription
```

The repository is a Python/uv monorepo:

| Package | Purpose | Maturity |
| --- | --- | --- |
| `fallow-protocol` | Versioned wire models and interface contracts | Implemented |
| `fallow-coordinator` | Registry, queue, scheduler, model distribution, gateway and app composition | Implemented |
| `fallow-agent` | Idle detection, preemption, supervision, cache, workers and runtime | Implemented |
| `fallow-cli` | `flw` operator CLI and admin API client | Implemented |
| `fallow-bench` | Workload, churn, experiment orchestration and analysis harness | Implemented |

The [architecture overview](docs/architecture.md) describes the system as built (component
diagram, request flows, module DAG, protocol versioning and trust model), and the
[scheduling-experiment protocol](docs/experiment.md) defines the research study, and the
[paper skeleton](docs/paper/README.md) provides result slots for the live runs. Individual
decisions are recorded in [`docs/adr/`](docs/adr/README.md). The [RAG query guide](docs/rag.md)
covers the retrieval API and Open WebUI setup. Protocol schemas are generated into
[`schemas/`](schemas/) and checked for drift in CI.

To run a coordinator and one agent through their first chat request, follow the
[quickstart guide](docs/quickstart.md).

For a small runnable introduction that does not require a coordinator, GPU or model download,
try the [protocol manifest example](examples/README.md).

## Start contributing

Prerequisites are Python 3.12 or 3.13, [uv](https://docs.astral.sh/uv/) and Git.

```bash
git clone https://github.com/Unluckyathecking/fallow.git
cd fallow
uv sync --frozen --dev
uv run pytest
```

Run the complete local quality gate before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
uv run python -m fallow_protocol.export_schemas schemas/ && git diff --exit-code -- schemas/
uv run pytest
uv build --all-packages
```

New contributors should read [CONTRIBUTING.md](CONTRIBUTING.md), browse
[good first issues](https://github.com/Unluckyathecking/fallow/labels/good%20first%20issue),
and consult the [compatibility policy](docs/compatibility.md) and
[API stability policy](docs/api-stability.md). Questions and proposals belong in
[GitHub Discussions](https://github.com/Unluckyathecking/fallow/discussions) when available,
or an issue otherwise.

## Security and support

Do not report vulnerabilities in public issues. Follow [SECURITY.md](SECURITY.md). Community
support expectations and the information to include in a help request are in
[SUPPORT.md](SUPPORT.md).

## License

Copyright is licensed under the [Apache License 2.0](LICENSE). Contributions are accepted
under the same license; see [CONTRIBUTING.md](CONTRIBUTING.md#licensing-and-developer-certificate-of-origin).
