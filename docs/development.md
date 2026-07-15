# Development guide

## Repository layout

Fallow is a uv workspace. Package source and tests live together under `packages/`; generated
wire schemas are committed under `schemas/`; time-bounded hardware investigations live under
`experiments/spikes/`; architecture decisions live under `docs/adr/`.

The enforced dependency direction is:

```text
fallow-protocol
      ^
      ├── fallow-coordinator
      ├── fallow-agent
      ├── fallow-cli
      └── fallow-bench
```

The coordinator and agent must not import one another. `fallow-protocol` must remain limited to
Pydantic and the standard library. `uv run lint-imports` enforces these boundaries.

## Common commands

```bash
uv sync --frozen --dev             # exact locked environment
uv run pytest                      # deterministic unit suite
uv run pytest path/to/test.py -k x # focused test
uv run ruff check . --fix          # safe lint fixes
uv run ruff format .               # formatter
uv run mypy                        # strict type checking
uv run lint-imports                # architecture contracts
uv build --all-packages            # wheel and source distributions
```

Do not hand-edit JSON schema files. Change protocol models and then regenerate them with:

```bash
uv run python -m fallow_protocol.export_schemas schemas/
```

Review the resulting diff and include it in the same commit.

## Testing principles

System time, process control, network transports, filesystem state and platform APIs should be
injected at module boundaries. Unit tests must not require a GPU, download a model or contact a
real service. Hardware spikes must publish the exact command, environment and limitations with
their results.
