# IT checklist — Fallow school pilot

For the IT team standing up a pilot fleet. It covers joining machines to the
tailnet, staging the inference binary, installing the agent so it starts at login
and restarts after a crash, clearing the endpoint-protection hurdles on Windows, and
installing without network access. It does not repeat the deployment reference; it
points at it. The full detail lives in [`deploy/README.md`](../../deploy/README.md) and
[`deploy/OFFLINE.md`](../../deploy/OFFLINE.md).

Fallow is pre-alpha and has not had a production security audit. Treat this pilot
as evaluation, not production, and read the [architecture trust
model](../architecture.md#52-identity-three-bearer-token-types--one-admin-key)
before granting anyone the admin key.

## What "tested" means here

The install scripts were written in a sandbox with no network and no macOS/Windows
service host. Every step that downloads a file or talks to `launchd` / Task
Scheduler is marked **(untested — verify on target)** in the script itself. The
Python packaging, hash verification and config handling are covered by tests; the
service registration and the downloads are not. Verify each of those on one real
machine of each kind before you roll out.

Below, **Tested** items are exercised by CI or the integration suite.
**Site-specific** items are yours to decide and verify locally.

## Prerequisites (every machine)

- **Tailscale**, joined to the pilot tailnet. Mandatory. v0.1 has no transport
  encryption of its own and delegates that to the tailnet (ADR 000 §6). Without
  it there is nothing protecting the coordinator API or the replica ports.
- **[uv](https://docs.astral.sh/uv/)**. Both installers build the virtualenv with it.
- **A git checkout of Fallow.** It is not published to PyPI in v0.1, so the install
  story is "clone the repo, `uv sync`, point the service at `.venv`." The offline
  bundle (below) is the exception — it carries its own wheels.
- **Python 3.12.** The offline installer refuses any other version.

## 1. Tailscale

- Join the coordinator and every agent to the same tailnet before anything else.
- Reach the coordinator by its tailnet IP or MagicDNS name, not a LAN address.
- Set each agent's `bind_host` (in `agent.toml`) to that machine's `100.x.y.z`
  tailnet address. Replica ports bind there only. The supervisor rejects a
  `0.0.0.0` bind outright — `llama-server` has no authentication of its own, so an
  all-interfaces bind would put an open inference endpoint on the office LAN.

*Site-specific:* tailnet ACLs, MagicDNS naming, and whether the coordinator gets a
stable tailnet name are yours to set.

## 2. Stage the llama.cpp binary

The agent supervisor launches `llama-server`. Fetch a pinned release before first run.

- **macOS:** `deploy/fetch-llama.sh` — downloads the `macos-arm64` build and records
  its SHA256 into `deploy/llama-version.lock`. Commit the lockfile so every Mac
  pins the identical bytes.
- **Windows:** `deploy\windows\fetch-llama.ps1` — picks the build for the machine
  (CUDA when it finds an NVIDIA GPU, CPU otherwise; `-Backend` overrides the probe)
  and verifies every download against the pinned hashes in
  `deploy\windows\llama-manifest.psd1` before unpacking anything. Pin the manifest
  once on a trusted staging machine — run `-UpdateManifest` twice, with
  `-Backend cuda` and with `-Backend cpu` — then commit it. On the CUDA path the
  script also fetches the matching `cudart` runtime DLLs: unpacking the CUDA build
  alone leaves `llama-server.exe` unable to start with a missing-DLL error, so if
  you stage by hand keep the two archives together and the CUDA sub-version
  matched.

llama.cpp publishes no per-asset checksum, so verify the pinned tag and asset names
against <https://github.com/ggml-org/llama.cpp/releases> before first use.

*Status:* the download step is untested in the sandbox — verify it once per platform.

## 3. Install the agent so it starts on login

The agent must run **inside the logged-in user's GUI session**. Idle detection
reads a per-session input timer that returns nothing from a system service in
session 0, so a service would read the machine as permanently idle and never yield
to the person using it. That constraint is the reason these installers exist.

- **macOS:** `deploy/macos/install.sh` builds the venv, copies
  `agent.example.toml` -> `~/.fallow/agent.toml` if absent, and loads a per-user
  **LaunchAgent** in `gui/$UID`. `KeepAlive` restarts it on exit. Logs land in
  `~/.fallow/logs/agent.out.log` and `agent.err.log`.
- **Windows:** `deploy\windows\install.ps1` bootstraps Python, builds the venv, and
  registers an **at-logon Scheduled Task** running `pythonw -m fallow_agent run` in
  the user session (`InteractiveToken`, least privilege, no console window).
  `RestartOnFailure` keeps it alive across crashes and preemption.

Both restart the agent after a crash within the logged-in session; neither survives
logout. On logout the GUI session tears down and the agent stops — it comes back at
the next login, not at boot before anyone has logged in. There is no service that
runs headless across reboots; that is deliberate (idle detection needs the GUI
session, above).

Edit the copied `agent.toml`: enrollment token (prefer the `FALLOW_ENROLLMENT_TOKEN`
env var so the secret is not written to disk), coordinator URL, tailnet `bind_host`,
and `llama_server_binary` pointing at the staged binary (a top-level key, or set
`FALLOW_LLAMA_SERVER_BINARY`).

Uninstall keeps `~/.fallow` unless you pass `--purge` (macOS) / `-Purge` (Windows).

*Status:* venv build and config handling are tested; the `launchd` / Task Scheduler
registration is untested in the sandbox — verify on one machine of each kind.

## 4. Defender / SmartScreen allowlisting (Windows — start early)

`llama-server.exe` is an unsigned third-party binary, and `pythonw.exe` spawning
children and binding sockets is the shape endpoint protection flags. In a managed
fleet this is an organizational conversation with lead time — days to weeks — not a
per-machine toggle. Work with the IT/security owner to:

- Allowlist the binary and its paths: `deploy\bin\windows\llama-server.exe`, the
  venv's `pythonw.exe`, and the `~\.fallow\` tree.
- Prefer a **hash-based** allow rule (Defender ASR / AppLocker / WDAC) for the
  staged `llama-server.exe` over a blanket path exclusion. The archives it was
  unpacked from are pinned in `deploy\windows\llama-manifest.psd1`, so the binary
  traces back to a verified download.
- Give SmartScreen an explicit reputation/allow entry for the unsigned download if
  it blocks it.
- Scope any inbound firewall rule for the replica ports to the **Tailscale adapter
  only**, never the LAN.

Full text is in [`deploy/README.md` §5.1](../../deploy/README.md#51-defender--smartscreen-allowlisting-plan-ahead--org-lead-time).
Code-signing the launcher is a later consideration (not in v0.1), so plan around
unsigned binaries for the pilot.

*Site-specific:* the exact rule type and approval path depend on your management
tooling (Intune, Group Policy, a third-party EDR). Decide locally.

## 5. Offline / air-gapped installation

For machines with no package-index or download access, install from the bundle
instead of a git checkout. See [`deploy/OFFLINE.md`](../../deploy/OFFLINE.md).

- The bundle carries locked Python wheels, pinned llama.cpp binaries for both agent
  platforms, and example configs. Model weights are **not** in the CI-built bundle;
  a local builder adds them with `deploy/bundle.sh build --output DIR --with-models DIR`
  (see [`deploy/OFFLINE.md`](../../deploy/OFFLINE.md) for the exact invocation).
- Run the preview first — `install.sh install --dry-run` / `install.ps1 Install
  -DryRun`. It verifies every hash in `manifest.sha256` and prints the target
  without touching it.
- The installer rejects unlisted files and unsafe paths, installs with `--no-index`,
  and leaves an existing `agent.toml` unchanged.

*Status:* CI builds the bundle and runs the install preview. A real install and the
service registration still need verifying on each target machine.

For a zero-egress lab, stage models once on the coordinator (`flw models pull ...`);
agents then pull blobs from the coordinator over the tailnet, so only the
coordinator needs egress. See [`deploy/README.md` §3.1](../../deploy/README.md#31-model-pre-staging-zero-egress-labs).

## Not yet available

Do not plan the pilot around these — they are on the [roadmap](../../ROADMAP.md),
not in v0.1:

- **Linux agents on ordinary user machines.** Only the coordinator runs on Linux;
  the agent support there is a benchmark-only scaffold, not a provisioned install.
- **Unattended install / upgrade paths** for a managed fleet.
- **Code-signed binaries** and **mTLS** (transport still relies solely on the
  tailnet).
