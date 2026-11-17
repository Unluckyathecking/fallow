# Restructure plan

Status: done (2026-11-17); see Outcome. Scope is cleanup only: no features, no history
rewrite, no changes under `.github/` or to `README.md`.

## Findings

### Layout

Six Python packages under `packages/` (`fallow-protocol`, `fallow-coordinator`,
`fallow-agent`, `fallow-modelmesh`, `fallow-cli`, `fallow-bench`), one Go module
(`go-agent/`, binary `agentctl`), installers under `deploy/`, JSON schemas under
`schemas/`, research harnesses under `experiments/`, and a manual chat UI under
`testbench/`. There is no Makefile; the commands are the ones CI runs:

- Python: `uv sync --frozen --dev`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run lint-imports`, `uv run pytest` (Linux, macOS, Windows; 3.12 and 3.13).
- Go: `gofmt -l`, `go test ./...` in `go-agent/`, plus `uv run pytest -m goagent` parity.
- Installers: `install-acceptance.yml` (macOS, Windows, Pester).

### Oversized source files (over 400 lines, tests excluded)

| Lines | File |
|---|---|
| 828 | `packages/fallow-coordinator/src/fallow_coordinator/registry/sqlite_registry.py` |
| 784 | `packages/fallow-coordinator/src/fallow_coordinator/queue/store.py` |
| 689 | `go-agent/cmd/agentctl/main.go` |
| 576 | `packages/fallow-coordinator/src/fallow_coordinator/app/factory.py` |
| 571 | `packages/fallow-bench/src/fallow_bench/experiment/live.py` |
| 529 | `packages/fallow-cli/src/fallow_cli/main.py` |
| 517 | `packages/fallow-coordinator/src/fallow_coordinator/site_relay/broker.py` |
| 509 | `go-agent/runtime/site.go` |
| 479 | `packages/fallow-coordinator/src/fallow_coordinator/gateway/proxy.py` |
| 470 | `packages/fallow-coordinator/src/fallow_coordinator/app/agent_routes.py` |
| 462 | `packages/fallow-coordinator/src/fallow_coordinator/gateway/service.py` |
| 428 | `packages/fallow-coordinator/src/fallow_coordinator/queue/_sql.py` |
| 425 | `go-agent/supervisor/supervisor.go` |
| 417 | `go-agent/runtime/runtime.go` |
| 411 | `packages/fallow-coordinator/src/fallow_coordinator/app/admin_routes.py` |

Longest functions: `build_admin_router` (245 lines, a closure over its routes),
`build_agent_router` (115), `Runtime.Run` in Go (95), `LiveRuntime.run` (92),
`SqliteQueueStore.submit_job` (88), `create_app` (87), `models_pull` (84).

### Duplication

- Bearer-token parsing exists three times: `fallow_coordinator/httpauth.py`
  (`extract_bearer`), `rag/query.py` and `gateway/service.py` (`_extract_bearer`).
- `_allows` is identical in `app/factory.py` and `gateway/service.py`.
- `_require_text` is identical in `rag/store.py` and `app/rag_ingestion.py`.
- Go: `trimTrailingSlash` is identical in `heartbeat/client.go` and `runtime/relay.go`;
  the bind-host guard is repeated in `config/config.go` and `supervisor/config.go`.
- Shell: `hash_file`, `validate_manifest_path` and `verify_bundle` are identical in
  `deploy/bundle.sh` and `deploy/site-bundle.sh`.

### Dead code

- `fallow_protocol/export_schemas.py`: `_is_wire_type` has no callers.
- `fallow_agent/main/runner_wiring.py`: `make_upload` has no callers.
- `gateway/bodyparse.py::parse_body` and `rag/retrieval.py::_embedding_from_response`
  are referenced only by tests.
- No leftovers from the reverted OCR worker.

### Docs drift

- `deploy/agent.example.toml` names `flw enroll create`; the command is `flw enroll new-token`.
- `deploy/llama-version.lock` is cited in three docs and does not exist.
- `docs/architecture.md` and `docs/adr/README.md` describe v0.1.0; packages are at 0.3.0.
  The architecture doc calls the Go agent a future port, and has no Site Mode or RAG section.
- `docs/development.md` omits `fallow-modelmesh` and `go-agent`.
- `docs/adr/README.md` indexes 60 of 99 records.
- `ROADMAP.md` and `SECURITY.md` carry statements the code has since overtaken
  (bench subcommands, enrollment-token revocation).

### Private or internal content in the tree

- `docs/adr/089-lan-site-acceptance.md`: a real user@host and tailnet address.
- `experiments/spikes/RESULTS.md`: two real tailnet addresses.
- A real site identifier in two runbooks and three test suites.
- `docs/adr/093-lan-site-pilot-runbook.md`: a tooling directive that is not part of the record.
- `OPEN_SOURCE_CHECKLIST.md`: a pre-publication to-do list for the owner.
- `AGENTS.md`: personal operating notes beyond the contributor-facing coding standard.

No secrets, personal emails, real names or home-directory paths were found.

### Network exposure

There are no compose files or Dockerfiles. Every shipped default binds loopback
(coordinator `127.0.0.1:8330`, replicas `127.0.0.1`, testbench `127.0.0.1:8770`),
and Site Mode rejects wildcard binds. One gap: outside Site Mode the Go agent
rejects only the literal `0.0.0.0`, while the Python agent rejects every
unspecified address.

### CI gaps (not changed here; `.github/` is out of scope)

- Go: no `go vet`, no `-race`, no linter, although ADRs 086 and 087 ask for race and vet.
- No coverage measurement for Python or Go.
- `experiments/moe/tests` is outside pytest `testpaths` and runs nowhere.
- `mypy` checks only the six packages, not `tests/`, `deploy/*.py` or `testbench/`.

## Ordered changes

Each item is one pull request, partitioned by path so they do not collide.

1. This plan.
2. Scrub private identifiers and internal notes from the tree.
3. Go agent: reject every unspecified bind host, matching the Python agent.
4. Fix docs drift (commands, missing files, versions, package lists, ADR index).
5. Remove duplicated helpers and dead code in the Python packages.
6. Move `agentctl doctor` out of `cmd/agentctl/main.go`.
7. Split `registry/sqlite_registry.py` along its existing sections.
8. Split `queue/store.py` along its existing sections.
9. Split `fallow_cli/main.py` by command group.
10. Record the outcome here.

## Outcome

| PR | Change |
|---|---|
| #156 | This plan. |
| #165 | Systemd unit test reads only lines about our own unit (unblocked CI on the current ubuntu image). |
| #157 | Removed a machine address, tailnet IPs, a real site identifier and the pre-publication checklist; trimmed `AGENTS.md`. |
| #158 | Go agent rejects every unspecified bind host, matching the Python agent. |
| #161 | Docs drift: command name, coordinator port, llama.cpp pin, versions, package lists, ADR index, roadmap and security statements. |
| #160 | Single definitions for bearer parsing, the model-allowlist check and `require_text`; removed `_is_wire_type` and `make_upload`. |
| #159 | `agentctl`: `doctor.go` and `wire.go` split out of `main.go` (689 to 183 lines). |
| #163 | Registry split into `base`, `credentials`, `agents`, `catalogue`; `sqlite_registry.py` 828 to 210 lines. |
| #164 | Queue store statement sequences moved to `_submission`, `_results`, `_requeue`, `_jobs`, `_backup`; `store.py` 784 to 459 lines. |
| #162 | `flw models` commands moved to `models_cmd.py`, shared state to `state.py`; `main.py` 529 to 304 lines. |

`deploy/llama-version.lock`, listed above as missing, is generated by
`deploy/fetch-llama.sh` on first run; the docs now say so.

## Deferred

- `app/factory.py` (573), `site_relay/broker.py` (517), `gateway/proxy.py` (479),
  `app/agent_routes.py` (470), `gateway/service.py` (450), `app/admin_routes.py` (411),
  `queue/store.py` (459) and `queue/_sql.py` (428): each is one cohesive unit (an app
  factory, a broker, a router closure, a SQL catalogue) only modestly over the guideline.
  Splitting them further would shuffle code without making it easier to read.
- `fallow_bench/experiment/live.py` (571) and the Go files `runtime/site.go`,
  `supervisor/supervisor.go`, `runtime/runtime.go`: same reasoning; candidates if they grow.
- Shared shell helpers in `deploy/bundle.sh` and `deploy/site-bundle.sh`: the scripts are
  copied into bundles and run standalone, so a sourced library would add a packaging
  dependency for about forty lines.
- Go duplicates (`trimTrailingSlash`, the bind-host guard in `config` and `supervisor`):
  small, and removing them needs a new shared package or a new import edge.
- `parse_body` and `_embedding_from_response` are used only by tests; left in place
  because the tests exercise them directly.
- The coordinator accepts a wildcard `host` outside Site Mode. The default is loopback and
  the docs tell operators to bind an exact address; rejecting it would be a behaviour
  change for existing configs and belongs in its own decision.
- RFC 1918 example addresses (`10.24.8.x`) remain in runbooks and fixtures; with the site
  identifier gone they identify nothing.
- CI changes (`go vet`, `-race`, coverage, `experiments/moe/tests`, wider `mypy` scope):
  listed under CI gaps above; `.github/` was out of scope.
- Git history still contains the removed identifiers; history was not rewritten.
