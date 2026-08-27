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
flw keys new NAME [--allow m1,m2] [--rpm N] [--per-day N]  # POST /api_keys
flw agents list                            # GET  /agents
flw models list                            # GET  /models
flw models register --file P --model-id M --family F --quant Q \
    [--worker-kind chat|embed|transcribe] [--min-vram-mb N] [--min-ram-mb N]
flw models pull SOURCE [--model-id M] [--family F] [--quant Q] [...]  # download then register
flw models pull --catalog ID                     # a curated entry, hash-verified
    # SOURCE is a URL or hf:<owner>/<repo>/<file.gguf> with an optional @<revision>
flw assign MODEL_ID AGENT_ID...            # PUT  /assignments
flw jobs submit --kind embed --model-id M --payload-ref REF   # POST /jobs
flw jobs status JOB_ID                     # GET  /jobs/{id}
flw status                                 # agents + models summary
flw site join-bundles --count N --output DIR [--force]   # POST /site/join-bundles
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
- **`pull` derives what the file can answer, and guesses no GPU.** With no
  `--quant`, the quantisation comes from the GGUF header's `general.file_type`;
  with no `--min-ram-mb`, the floor is `ceil(size/MiB * 1.15) + 512`. Operator
  flags beat the catalog, and the catalog beats anything derived. `min_vram_mb`
  stays `0` unless declared, because a non-zero value is what makes ADR 048
  auto-assign prefer a GPU desk. A header that will not parse falls back to the
  flags with a message naming the reason (never a crash, never a failed
  download). Only the coordinator host dials huggingface.co; see
  [ADR 103](../../../../docs/adr/103-hf-model-staging.md).
- **Immutable wire types.** All request/response bodies are frozen
  `FallowModel`s (`extra="forbid"`), so protocol drift fails loudly at parse time.
- **Site join files never leak secrets.** `site join-bundles` uses a direct
  no-proxy client and validates every bundle against the strict v1 contract —
  HTTPS-only origins (no userinfo, path, query, fragment, or out-of-range port)
  and canonical `sha256/` pins — the same rules the Go site-client enforces, so
  a written bundle always parses there. It refuses to clobber existing files
  without `--force`, and that refusal is checked **before** any one-use token is
  minted; the write itself is an atomic, no-clobber `os.link` so two concurrent
  invocations can never both create the same file. A `--force` batch that fails
  part-way rolls back to the previous files, and a backup it cannot restore is
  kept on disk with its location reported rather than deleted. Neither the human
  nor `--json` output prints enrollment tokens or full bundle contents — only
  paths, site ID, coordinator origins and a short pin prefix.
- **Owner-only join files on every OS.** Each file is written owner-only:
  `chmod 0o600` on POSIX, and on Windows an explicit DACL (via `icacls`) that
  removes inheritance and grants full control to only the current account, so
  `Users`, `Authenticated Users` and `Everyone` get no access.

## Files

- `main.py` — typer app, global options, command wiring, transport seams.
- `client.py` — `AdminClient` (one method per admin route).
- `config.py` — configuration resolution + validation.
- `models.py` — admin request/response bodies (CLI half of the contract).
- `blobs.py` — sha256 hashing, streaming download, manifest construction.
- `pull.py` — `models pull` resolution: source or catalog id in, manifest fields out.
- `hf.py` — `hf:<owner>/<repo>/<file.gguf>[@<revision>]` → a resolve URL.
- `gguf.py` — header-only GGUF reader (stdlib) + the derived RAM floor.
- `catalog.py` + `model_catalog.toml` — the curated, hash-verified model list.
- `render.py` — rich tables + `--json` rendering.
- `errors.py` — `CliError` + exit codes.
- `site/` — Site Mode join-file writer (`write_join_bundles`).
