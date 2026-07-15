# fallow-cli — the `flw` admin CLI (module L1)

`flw` is the operator's command line for a Fallow deployment. It talks **only**
to the coordinator admin API (`/v1/admin/*`) over HTTP + JSON and depends on
`fallow_protocol` + `typer` + `rich` + `httpx` only (import-linter forbids any
`fallow_coordinator` / `fallow_agent` import). The admin-API contract itself is
specified in [`docs/admin-api.md`](../../../../docs/admin-api.md); wave-3
implements the coordinator side against it.

## Public API

Re-exported from `fallow_cli`:

- `app` — the typer application (entry point `fallow_cli.main:app`, script `flw`).
- `AdminClient(client: httpx.Client, admin_key: str)` — one typed method per
  admin route; every HTTP failure becomes a friendly `CliError`.
- `load_config(cli_url, env, *, config_path=None) -> CliConfig` — pure config
  resolution; `require_admin_key(config)` fetches the key or explains how to set it.
- `CliError(message, *, exit_code=1)` — the user-facing error type.

## Commands

```
flw enroll new-token                       # POST /enrollment_tokens
flw keys new NAME [--allow m1,m2]          # POST /api_keys
flw agents list                            # GET  /agents
flw models list                            # GET  /models
flw models register --file P --model-id M --family F --quant Q \
    [--worker-kind chat|embed|transcribe] [--min-vram-mb N] [--min-ram-mb N]
flw models pull URL --model-id M --family F --quant Q [...]   # download then register
flw assign MODEL_ID AGENT_ID...            # PUT  /assignments
flw jobs submit --kind embed --model-id M --payload-ref REF   # POST /jobs
flw jobs status JOB_ID                     # GET  /jobs/{id}
flw status                                 # agents + models summary
```

`--coordinator-url` and `--json` are **global** options (before the subcommand):
`flw --json agents list`.

## Configuration & secrets

- **Coordinator URL**: `--coordinator-url` → `FLW_COORDINATOR_URL` → `coordinator_url`
  in `~/.fallow/cli.toml` (override the path with `FLW_CONFIG_FILE`).
- **Admin key**: `FLW_ADMIN_KEY` env → `admin_key` in the config file. There is
  **no** admin-key flag — a flag would leak the secret into shell history and
  process listings.

## Invariants

- **No network in tests / deterministic.** The HTTP transports are injected
  (`_ADMIN_TRANSPORT`, `_DOWNLOAD_TRANSPORT`); tests drive them with
  `httpx.MockTransport`. Nothing dials a real coordinator, llama-server, or GPU.
- **Friendly failures, no tracebacks.** Expected errors raise `CliError`, print
  their message to stderr, and exit non-zero (`2` for auth/config, `1` otherwise):
  `401/403 → "admin key rejected"`, connect error → `"coordinator unreachable at <url>"`.
- **sha256 computed locally.** `register` / `pull` stream the blob to compute
  `sha256` + `size_bytes`, build a validated `ModelManifest`, and POST it with an
  absolute `blob_path`. v0.1 assumes the CLI runs on the coordinator host.
- **Immutable wire types.** All request/response bodies are frozen
  `FallowModel`s (`extra="forbid"`), so protocol drift fails loudly at parse time.

## Files

- `main.py` — typer app, global options, command wiring, transport seams.
- `client.py` — `AdminClient` (one method per admin route).
- `config.py` — configuration resolution + validation.
- `models.py` — admin request/response bodies (CLI half of the contract).
- `blobs.py` — sha256 hashing, streaming download, manifest construction.
- `render.py` — rich tables + `--json` rendering.
- `errors.py` — `CliError` + exit codes.
