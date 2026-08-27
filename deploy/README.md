# Fallow deployment (v0.1)

Scripts and service definitions that stage the `llama.cpp` binary and install the
Fallow **coordinator** and **agents** as long-running background processes.

> **Honesty note.** These scripts were authored in a sandbox with **no network
> access and no Windows/macOS service host**. Every step that downloads a file or
> talks to `launchd` / Task Scheduler is annotated in the script itself: the steps
> `.github/workflows/install-acceptance.yml` now runs for real on `windows-latest`
> and `macos-latest` say **(exercised in CI on … — verify on target)**, and the
> rest still say **(untested — verify on target)**. A hosted runner is not a desk:
> it proves registration and removal, not that the task starts at a real logon,
> that an agent serves, or that EDR and SmartScreen let it. Verify the pinned
> llama.cpp tag and asset names against
> <https://github.com/ggml-org/llama.cpp/releases> before first use.

## Offline bundle

`bundle.sh build` stages a zero-egress installation directory from `uv.lock`.
It contains workspace and dependency wheels for Python 3.12, pinned macOS and
Windows llama.cpp binaries, the Windows CUDA runtime DLLs, configuration
examples, and optional model weights. CI leaves model weights out. Use
`--with-models DIR` for a local bundle that includes them.

Both installers verify the complete `manifest.sha256` before changing the
target. Their preview modes run the same verification without creating the
target directory. See [the bundle guide](OFFLINE.md) for commands and the
remaining target-machine checks.

---

## One-shot bootstrap

`bootstrap.sh` (macOS) and `bootstrap.ps1` (Windows) turn a fresh machine into
an enrolled agent in a single command. They are thin orchestrators: they read
the machine (OS, CPU arch, RAM, GPU), pick the backend (Metal / CUDA / CPU),
and hand off to the per-OS installer below with the matching flags. The venv
build, the SHA256 verification, and the service wiring all stay in
`install.sh` / `install.ps1` — the bootstrap adds nothing to that path and
relaxes none of it.

```bash
# macOS — Python venv flavour, enrol with a one-time token
FALLOW_ENROLLMENT_TOKEN=<token> deploy/bootstrap.sh
deploy/bootstrap.sh --go-binary /path/to/agentctl --token <token>   # Go flavour
deploy/bootstrap.sh --dry-run                                        # detect + delegate, change nothing
```

```powershell
# Windows — same shape
$env:FALLOW_ENROLLMENT_TOKEN = '<token>'; deploy\bootstrap.ps1
deploy\bootstrap.ps1 -GoBinary C:\path\to\agentctl.exe -Token <token>
deploy\bootstrap.ps1 -WhatIf
```

The token is kept in memory, fed to the agent's first run, and cleared once the
agent has registered — it is never written to disk, and the agent persists only
its identity, not the token. After install the bootstrap runs a local self-test
(service loaded, config present) and reports success or failure. Staging the
llama.cpp binary (§2) is still a prerequisite. ADR 062 records the design.

---

## 0. Support matrix

| Role            | macOS (Apple Silicon) | Windows x64 (CUDA)      | Linux           |
| --------------- | --------------------- | ----------------------- | --------------- |
| **Coordinator** | ✅ supported          | — (not targeted in v0.1) | ✅ supported     |
| **Agent**       | ✅ supported          | ✅ supported             | benchmark scaffold only |

- **Coordinator** is a plain long-running process (`fallow_coordinator.app` +
  `uvicorn`). It has no idle/GUI-session constraint, so it runs equally well on
  macOS or Linux. On Linux, `deploy/coordinator/install.sh` installs it as a
  systemd service in one command (§3); on a Mac, run it under `launchd` using the
  same pattern as the agent plist.
- **Agents** must run **inside the logged-in user's GUI session** on both
  macOS and Windows — see the "why user session" boxes below. That is the whole
  reason this module exists rather than shipping a system service.

Linux agents remain unsupported for ordinary user machines. A headless experiment host
may use the guarded benchmark-only constant idle detector and the provider-neutral files
in [`experiments/fleet/`](../experiments/fleet/README.md). Those files do not provision a
machine or replace the operator's tailnet and secret-management process.

On the Go agent such a host also needs `assume_idle = true` in its `agent.toml`,
which lifts the refusal to start without idle detection and is only ever right
where nobody uses the machine, never a desk. The key is Go-only; the Python agent
rejects it.

---

## 1. Prerequisites (all machines)

### 1.1 Tailscale (mandatory in v0.1)

Per **ADR 000 §6**, v0.1 has **no transport encryption of its own** — it delegates
that to the tailnet. Every machine (coordinator + all agents) **must** be joined
to the same Tailscale tailnet before anything else:

- The coordinator's admin/gateway API is reached over its tailnet IP/MagicDNS name.
- Each agent's llama.cpp **replica ports bind to the agent's tailnet IP only**
  (`supervisor.bind_host` in the agent config). They are **never** bound to
  `0.0.0.0` — `llama-server` has no authentication, so an all-interfaces bind
  would expose an open inference endpoint on the office LAN. The supervisor
  config rejects `0.0.0.0` outright.

Set each agent's `bind_host` to the machine's `100.x.y.z` Tailscale address.

### 1.2 uv

Both installers use [uv](https://docs.astral.sh/uv/) to build a virtualenv from a
**git checkout** of this monorepo (Fallow is not published to PyPI in v0.1, so the
honest install story is "clone the repo, `uv sync` it, point the service at
`.venv`"). Install uv first.

### 1.3 A git checkout

Clone the Fallow repo onto each machine. The installers default to the checkout
they live in; override with the first positional arg (`install.sh <repo>`) or
`-RepoRoot` (Windows) / the `FALLOW_REPO` env var.

---

## 2. Stage the llama.cpp binary

The agent's process supervisor launches `llama-server` (path =
`supervisor.llama_binary` in the agent config). Fetch a **pinned** release into
`deploy/bin/<platform>/`:

### macOS

```bash
deploy/fetch-llama.sh
```

Downloads the `macos-arm64` zip for the pinned tag, records its SHA256 into
`deploy/llama-version.lock`, and unpacks `llama-server` (plus bundled dylibs) into
`deploy/bin/macos/`.

### Windows

```powershell
deploy\windows\fetch-llama.ps1
```

Picks the build for the machine — CUDA when it finds an NVIDIA GPU, CPU
otherwise (`-Backend cuda|cpu` overrides the probe) — verifies every download
against the pinned hashes in `deploy\windows\llama-manifest.psd1`, and unpacks
into `deploy\bin\windows\`. On the CUDA path it fetches **two** archives:

1. `llama-…-bin-win-cuda-cu12.4-x64.zip` — the CUDA build, **and**
2. `cudart-llama-bin-win-cu12.4-x64.zip` — the CUDA runtime DLLs.

> ⚠️ **The classic trap.** The `win-cuda` archive does **not** contain
> `cudart64_*.dll` / `cublas64_*.dll`. If you unpack only the first zip,
> `llama-server.exe` dies at launch with a missing-DLL error. You **must** unpack
> the matching `cudart-…` zip into the same folder. The CUDA sub-version of the
> two archives must match (both `cu12.4`). `fetch-llama.ps1` fetches and unpacks
> both for you. The CPU build needs neither.

### Pinning & verification

The release tag lives in **one variable** at the top of each fetch script
(`LLAMA_RELEASE` on macOS; `$LlamaRelease` plus the matching `$CudaTag` on
Windows). Bump it there to move builds. llama.cpp publishes no per-asset
checksum file, so each platform keeps its own trusted record:

- **macOS**: `fetch-llama.sh` **records** the downloaded SHA256 into
  `deploy/llama-version.lock` on first run and **verifies against it** on later
  runs — commit the lockfile so every Mac pins the identical bytes.
- **Windows**: `fetch-llama.ps1` verifies every download against
  `deploy\windows\llama-manifest.psd1` and refuses anything unpinned or
  altered. Pin the manifest once on a trusted staging machine — run
  `-UpdateManifest` twice, with `-Backend cuda` and with `-Backend cpu` — then
  review the diff and commit it.

---

## 3. Coordinator

Run the coordinator on a machine that stays up (a Linux box or a Mac mini). It
serves the admin API and the OpenAI-compatible gateway.

### Linux: one command, a systemd service

`deploy/coordinator/install.sh` is the supported Linux path. It needs `git` and
[uv](https://docs.astral.sh/uv/) already installed, root, and **egress**: it
clones from github.com, and `uv sync` downloads the wheels it has not cached plus
a managed CPython 3.12 (this workspace pins `python-preference = "only-managed"`,
so a system python is not used even at the right version). A zero-egress lab
cannot install this way. Use the offline bundle ([OFFLINE.md](OFFLINE.md)).
Deploy a **pinned release tag**. It refuses a branch unless you pass
`--allow-branch`:

```bash
sudo deploy/coordinator/install.sh --ref v0.3.0
sudo deploy/coordinator/install.sh --ref v0.3.0 --dry-run   # print the plan, change nothing
```

No checkout on the host yet? The script clones one itself, so fetch it alone from
the tag you are deploying, read it, then run it. Nothing here pipes an installer
into a root shell:

```bash
curl -fsSLO https://raw.githubusercontent.com/Unluckyathecking/fallow/v0.3.0/deploy/coordinator/install.sh
sudo bash install.sh --ref v0.3.0
```

It creates the `fallow` system user, checks the repo out at that ref under
`/opt/fallow/src`, builds the venv with `uv sync --frozen --no-dev` (the managed
CPython lands in `/opt/fallow/python` via `UV_PYTHON_INSTALL_DIR`, where the
service user can read it — uv's default is the invoking user's private data
directory, behind a `/root` the `fallow` user cannot traverse), puts state in
`/var/lib/fallow` and config in `/etc/fallow/coordinator.toml` (copied from
`coordinator.example.toml` **only if absent**; it never overwrites a live
config), and installs `fallow-coordinator.service`. An existing config keeps its
contents and gets its ownership and mode reset to `root:fallow 0640` — a
`root:root 0600` copy the service user cannot read, and a `0644` one every local
account can, are both silent until they bite.

**A run that would start the coordinator with the example's published
placeholder admin key installs the unit and stops there.** That is the first run,
which seeds the config, and any later run whose `admin_key` is still the
placeholder or empty — editing `host` and the TLS paths and leaving the key alone
is the easy mistake. Set the key one of two ways, then re-run the installer (it
starts the service once a key is set), or `systemctl enable --now
fallow-coordinator`:

- `admin_key` in `/etc/fallow/coordinator.toml`, which is group-readable by the
  service user; or
- `FALLOW_COORD_ADMIN_KEY=` in `/etc/fallow/coordinator.env`, which the installer
  seeds root-only (`root:root 0600`) with that line commented out. The unit reads
  it via `EnvironmentFile=`, systemd reads it as root before dropping to the
  `fallow` user, and an environment value wins over the config file. This is the
  way to keep the key out of a file the service user can read. Every
  `FALLOW_COORD_*` override works there; a service inherits none of your shell
  environment, so this file is the only place they reach it.

Also edit `host` (the exact address to serve on) and the `[site]` certificate
paths for a Site Mode pilot.

Re-running it with a newer `--ref` is the **upgrade**: stop the running service,
fetch, check out, re-sync, start. It stops first because the venv runs the code
straight out of `/opt/fallow/src`. `--no-start` installs the unit without
enabling it.

```bash
sudo deploy/coordinator/install.sh uninstall            # stop, remove unit + /opt/fallow/{src,python}
sudo deploy/coordinator/install.sh uninstall --purge     # also delete /etc/fallow + /var/lib/fallow
```

The unit runs as `fallow` with `NoNewPrivileges`, `ProtectSystem=strict` (plus
`ReadWritePaths=/var/lib/fallow`) and `PrivateTmp`, and restarts on failure.
`/var/lib/fallow` is therefore the **only** path the service can write. A warm
standby (§3.2) pointed anywhere else needs that directory added as a drop-in:

```bash
sudo systemctl edit fallow-coordinator.service   # [Service] / ReadWritePaths=/mnt/standby
```

The installer refuses a config whose `standby_path` sits outside
`/var/lib/fallow` unless you pass `--allow-external-standby` to say the drop-in
is in place. Without it every export fails and only the journal notices.
[ADR 100](../docs/adr/100-coordinator-systemd-install.md) records the decision
and its gaps: chiefly that it is Linux-only and was authored without a systemd
host to run it on.

### Manual, or on macOS

The checkout path still works everywhere and is what a Mac uses. Copy
`deploy/coordinator.example.toml` to `~/.fallow/coordinator.toml`, then:

```bash
cd <fallow checkout>
uv sync --no-dev
.venv/bin/python -m fallow_coordinator serve --config ~/.fallow/coordinator.toml
```

`serve` reads the bind address from the config and, under Site Mode, passes the
configured TLS certificate and key to uvicorn. The older
`uvicorn fallow_coordinator.app:build_app --factory` form still works for the
legacy HTTP setup (explicit URL or Tailscale, Site Mode disabled), but it cannot
apply the Site Mode certificate or exact bind, so it fails closed under Site Mode
and points you back to `serve`.

Keeping a Mac coordinator alive across reboots follows the same `launchd` pattern
as the agent plist below and is left to the operator in v0.1.

### 3.1 Model pre-staging (zero-egress labs)

For air-gapped / zero-egress offices, stage models **once on the coordinator**;
agents then pull blobs **from the coordinator**, never from the public internet:

```bash
# On the coordinator host: download + register a model blob.
flw models pull --catalog qwen2.5-0.5b-instruct-q4km

# Or any GGUF on the Hub, by owner/repo/file (append @<revision> to pin one):
flw models pull hf:Qwen/Qwen2.5-0.5B-Instruct-GGUF/qwen2.5-0.5b-instruct-q4_k_m.gguf \
    --model-id qwen2.5-0.5b-instruct --family qwen2.5

# A plain URL still works, and still takes every flag by hand.
flw models pull <source-url> \
    --model-id qwen2.5-7b-instruct-q4 \
    --family qwen2.5 --quant Q4_K_M --worker-kind chat
```

`flw models pull` streams the blob into the coordinator's `~/.fallow/blobs` and
registers its manifest. `--quant` and `--min-ram-mb` are read out of the GGUF
header and the file size when they are not given; `--min-vram-mb` stays `0` (CPU)
unless you pass it, because a non-zero value is what makes auto-assign prefer a
GPU desk. A `--catalog` entry additionally verifies the download against a
recorded sha256 and refuses on a mismatch. See
[ADR 103](../docs/adr/103-hf-model-staging.md).

When a model is assigned to an agent, the agent's model
cache pulls the blob **from the coordinator's blob endpoint** over the tailnet, so
the only machine that needs egress is the coordinator (and even that can be primed
off a USB drive by dropping files into `~/.fallow/blobs` and registering with the
local path).

### 3.2 Warm standby and failover (optional)

The coordinator is a single point of failure. To mitigate it, set `standby_path`
in `coordinator.toml` to a location a second host can read (a synced path over the
tailnet). The coordinator then ships a consistent snapshot of its state DB there
every `standby_export_interval_s` (default 60s). The feature is off unless
`standby_path` is set, and `standby_path` must differ from `db_path`. Under the
systemd unit it must also be a path the unit is allowed to write; see the
`ReadWritePaths` drop-in above.

On coordinator loss, failover is a manual two-command step on the standby host,
run with no coordinator running there:

```bash
# Install the last snapshot as this host's live state DB, then serve from it.
.venv/bin/python -m fallow_coordinator promote --config ~/.fallow/coordinator.toml
.venv/bin/python -m fallow_coordinator serve   --config ~/.fallow/coordinator.toml
```

`promote` validates the snapshot and refuses to overwrite a `db_path` newer than
it (use `--force` to override once the primary is confirmed down). Up to one export
interval of state can be lost. You then point operators and agents at the new host
by hand — automatic agent re-pointing is a later increment. The full procedure,
including the pre-flight config on both hosts, is in the
[administrator runbook](../docs/pilot/admin-runbook.md#failover-coordinator-down);
the design is ADR 054 (export) and ADR 057 (promote).

---

## 4. Agent — macOS

```bash
deploy/fetch-llama.sh          # once, stages llama-server
deploy/macos/install.sh        # builds .venv, installs the LaunchAgent
```

`install.sh`:

1. `uv sync --no-dev` in the checkout → `.venv`.
2. Copies `deploy/agent.example.toml` → `~/.fallow/agent.toml` **if absent** (edit
   it: enrollment token, coordinator URL, tailnet `bind_host`, and
   `supervisor.llama_binary` → `deploy/bin/macos/llama-server`).
3. Renders `com.fallow.agent.plist` → `~/Library/LaunchAgents/` and loads it with
   `launchctl bootstrap gui/$UID`.

> **Prebuilt Go binary instead of the Python venv.** Pass `--go-binary <path>`
> to install a released `agentctl` (see §7) as the agent:
>
> ```bash
> deploy/macos/install.sh --go-binary /path/to/agentctl
> ```
>
> This skips step 1 entirely (no uv, no venv, no repo checkout needed): the
> binary is copied to `~/.fallow/bin/agentctl` and the LaunchAgent runs
> `agentctl run --config ~/.fallow/agent.toml`. Steps 2 (config) and 3 (plist +
> launchctl) are unchanged, and the llama-server staging in §2 is still required.
> Use the `darwin_arm64` archive from a GitHub Release (§7) or a local `go build`
> on a Mac: idle detection needs cgo, and `agentctl run` refuses to start on a
> build without it rather than serve through your day.

> **Why a LaunchAgent, not a LaunchDaemon.** Idle detection reads the console
> user's HID idle timer (`CGEventSourceSecondsSinceLastEventType` via pyobjc
> Quartz). That API only returns meaningful values inside a logged-in **Aqua GUI
> session**. A LaunchDaemon runs in system context (session 0) with no window
> server, so it would always read "idle" and Fallow would never yield to the
> user. Hence a per-user LaunchAgent in `gui/$UID`.

Logs: `~/.fallow/logs/agent.out.log` and `agent.err.log` (wired via the plist's
`StandardOutPath`/`StandardErrorPath`). `KeepAlive` restarts the agent on exit.

**Uninstall:**

```bash
deploy/macos/uninstall.sh          # remove the service, keep ~/.fallow
deploy/macos/uninstall.sh --purge  # also delete ~/.fallow
```

---

## 5. Agent — Windows

```powershell
deploy\windows\fetch-llama.ps1     # stages the hash-verified build for this machine
deploy\windows\install.ps1         # bootstraps python, installs the task
```

`install.ps1`:

1. `uv python install 3.12` then `uv sync --no-dev` → `.venv`.
2. Copies `deploy\agent.example.toml` → `%USERPROFILE%\.fallow\agent.toml` if
   absent (edit the same fields as macOS; point `llama_binary` at
   `deploy\bin\windows\llama-server.exe`).
3. Renders `fallow-agent-task.xml` and registers it as an **at-logon Scheduled
   Task** running `pythonw -m fallow_agent run` in the user session.

> **Prebuilt Go binary instead of the Python venv.** Pass `-GoBinary <path>` to
> install a released `agentctl.exe` (see §7) as the agent:
>
> ```powershell
> deploy\windows\install.ps1 -GoBinary C:\path\to\agentctl.exe
> ```
>
> This skips step 1 (no uv, no venv, no repo checkout needed): the binary is
> copied to `%USERPROFILE%\.fallow\bin\agentctl.exe` and the task runs
> `agentctl run -config "%USERPROFILE%\.fallow\agent.toml"`. Steps 2 and 3 are
> unchanged, and the llama-server staging in §2 (plus the allowlisting in §5.1)
> still applies. `agentctl.exe` is a console binary with no `pythonw`-style
> windowless launcher, so a brief console window at logon is possible; a
> windowless wrapper is a v0.2 consideration alongside code-signing.

> **Why a Scheduled Task in the user session, not a Windows Service.** Idle
> detection calls `GetLastInputInfo` (user32), which reports the last input for
> the **active user session**. A Windows Service runs in the isolated session 0
> with no interactive input desk, so `GetLastInputInfo` is useless there and
> Fallow would never yield. The task therefore uses
> `LogonType=InteractiveToken`, `RunLevel=LeastPrivilege`, and `pythonw.exe` (no
> console window). `RestartOnFailure` (1-minute interval) keeps it alive across
> crashes/preemption.

### 5.1 Defender / SmartScreen allowlisting (plan ahead — org lead time)

`llama-server.exe` is an unsigned third-party binary, and `pythonw.exe` spawning
child processes plus binding sockets is exactly the shape Defender/SmartScreen and
many EDR agents flag. In a managed-fleet office this is an **organizational**
conversation with **lead time**, not something to disable per-machine. Before a
rollout, work with IT to:

- **Allowlist the binaries/paths**: `deploy\bin\windows\llama-server.exe`, the
  `.venv\Scripts\pythonw.exe`, and the `~\.fallow\` tree (models + blobs).
- **Publisher/hash rules**: prefer a hash-based Defender ASR / AppLocker / WDAC
  allow rule for `llama-server.exe` over blanket path exclusions. The archives
  it is unpacked from are pinned in `deploy\windows\llama-manifest.psd1`, so
  the binary traces back to a verified download.
- **SmartScreen**: unsigned downloads may need an explicit reputation/allow entry;
  code-signing the launcher is a v0.2 consideration.
- **Firewall**: replica ports must be reachable **on the Tailscale interface
  only** — scope any inbound rule to the tailnet adapter, not the LAN.

Budget days-to-weeks for security review; do not assume a silent install.

**Uninstall:**

```powershell
deploy\windows\uninstall.ps1          # remove the task, keep ~\.fallow
deploy\windows\uninstall.ps1 -Purge   # also delete ~\.fallow
```

### 5.2 LAN Site Mode (Windows, opt-in)

Site Mode is the second deployment shape: an on-site coordinator on the LAN,
Windows Go agents reaching it over pinned HTTPS, no Tailscale and no internet. It
is opt-in and additive — everything in §1 to §5 above is unchanged for a machine
that does not use it.

The install is the same `bootstrap.ps1`, plus a join file:

```powershell
deploy\bootstrap.ps1 -JoinBundle D:\join\desk-01.fallow-join -GoBinary C:\tools\agentctl.exe
```

`-JoinBundle` requires `-GoBinary`: the Python agent has no Site Mode. The join
file is validated before anything is written, copied to
`%USERPROFILE%\.fallow\site\join.json` with an owner-only ACL, and the rendered
`agent.toml` is token-free with `bind_host = "127.0.0.1"`.

Check the result with `deploy\windows\doctor.ps1`, which reports the Scheduled
Task, the logged-in session, file ACLs, the loopback bind, the llama binary, the
stored identity, idle detection, the pinned-TLS check and the clock offset as one
JSON object.
Add `-Probe` for a live reach test that tells a blocked port, a TLS-intercepting
proxy and a pin mismatch apart.

Three things differ from the tailnet path and matter for deployment:

- **No inbound rule anywhere.** Agents dial the coordinator; the coordinator never
  dials an agent. Do not open a port for `llama-server` — its replicas listen on
  `127.0.0.1` only, and the config refuses a non-loopback bind in Site Mode.
- **TLS interception breaks it by design.** Trust is the pinned SubjectPublicKeyInfo
  hash from the join file, not the Windows trust store. Exempt the coordinator host
  from inspection; never relax the pin.
- **The join file is a credential.** Single-use token, no expiry, live until used.
  Destroy the media once the machine is enrolled.

### The desk bundle

A pilot desk should not need a checkout of this repository. Every release
carries `fallow-site-agent_<version>_windows_amd64.zip`: the released
`agentctl.exe`, `bootstrap.ps1`, the Windows scripts above, an operator
`README.md`, and a `manifest.sha256` covering all of it. A desk unzips that,
stages llama.cpp, and runs one install command: `bootstrap.ps1`, which resolves
`windows\install.ps1` from beside itself, so it works from the bundle exactly as
it does from a checkout. Model weights are not in it and llama.cpp is not
either. `windows\fetch-llama.ps1` downloads that, or it is staged by hand.

`site-bundle.sh` builds it, and verifies it the same way `bundle.sh` verifies the
offline bundle:

```bash
deploy/site-bundle.sh build --agent path/to/agentctl.exe --version 0.1.0 --output dist
deploy/site-bundle.sh verify dist/fallow-site-agent_0.1.0_windows_amd64
```

`verify` rejects a changed file, an unlisted file, an unsafe or duplicate
manifest path, a symbolic link and anything that is not a regular file. `build`
refuses to write over an existing `<name>` directory or `<name>.zip`, the same
refusal `bundle.sh` makes. Delete the old build or pass another `--output`.
CI builds a bundle on every push, unzips it and verifies what came out of the
zip; the release workflow publishes one built from the released Windows archive
and verifies it the same way. See
[ADR 099](../docs/adr/099-site-desk-bundle.md).

Windows detail is in [`windows/JOIN-README.md`](windows/JOIN-README.md) and
[`windows/README.md`](windows/README.md). The end-to-end operator procedure —
address, certificate, join files, doctor, assignment, preemption, restart,
revocation, rollback and removal — is
[`docs/lan-site/operator-runbook.md`](../docs/lan-site/operator-runbook.md).

---

## 6. Files in this directory

| Path                              | Purpose                                                        |
| --------------------------------- | ------------------------------------------------------------- |
| `site-bundle.sh`                  | Build + verify the one-zip Site Mode desk bundle.              |
| `SITE-BUNDLE.md`                  | The desk bundle's operator README, shipped as its `README.md`. |
| `coordinator/install.sh`          | Linux: install/upgrade/remove the coordinator systemd service. |
| `coordinator/fallow-coordinator.service` | The systemd unit, copied verbatim by that script. Its `EnvironmentFile` is `/etc/fallow/coordinator.env`, seeded root-only by that script. |
| `bootstrap.sh`                    | macOS: detect + select backend + delegate to `macos/install.sh`. |
| `bootstrap.ps1`                   | Windows: detect + select backend + delegate to `windows/install.ps1`. |
| `fetch-llama.sh`                  | macOS: fetch + unpack pinned llama.cpp `macos-arm64`.         |
| `windows/fetch-llama.ps1`         | Windows: fetch + verify + unpack the CUDA or CPU build.       |
| `macos/install.sh`                | Install agent as a `launchd` LaunchAgent (user session).      |
| `macos/uninstall.sh`              | Remove the LaunchAgent (`--purge` to delete `~/.fallow`).     |
| `macos/com.fallow.agent.plist`    | LaunchAgent template (tokens filled by `install.sh`).         |
| `macos/render_test.sh`            | Asserts `install.sh` wires the right agent (Python vs Go).    |
| `windows/install.ps1`             | Install agent as an at-logon Scheduled Task (user session).   |
| `windows/uninstall.ps1`           | Remove the task (`-Purge` to delete `~\.fallow`).            |
| `windows/fallow-agent-task.xml`   | Task Scheduler template (tokens filled by `install.ps1`).     |
| `agent.example.toml`              | Example agent config (provided by the config module).         |
| `coordinator.example.toml`        | Example coordinator config (provided by the config module).   |
| `windows/llama-manifest.psd1`     | Windows: pinned asset SHA256s — pin once, commit it.          |
| `windows/lib/backend.ps1`         | Windows: CUDA/CPU detection + CPU thread cap helpers.         |
| `windows/doctor.ps1`              | Windows: read-only Site Mode diagnosis, one JSON report.      |
| `windows/new-site-config.ps1`     | Windows: validate a join file, render its token-free config.  |
| `windows/JOIN-README.md`          | Windows: installing a Site Mode agent from a join file.       |
| `windows/site-join.schema.json`   | Join file v1 schema, used to validate before install.         |
| `llama-version.lock`              | macOS: generated on first fetch; pins asset SHA256s — commit. |
| `bin/<platform>/`                 | Fetched llama.cpp binaries (git-ignored, per-host).           |

---

## 7. Go agent releases

The Go agent binary (`agentctl`) is released with GoReleaser from a `v*` git
tag. Config: [`go-agent/.goreleaser.yaml`](../go-agent/.goreleaser.yaml).
Workflow: [`.github/workflows/release.yml`](../.github/workflows/release.yml).

- Every pull request touching `go-agent/` runs `goreleaser check` and a snapshot
  cross-build for `windows/amd64` and `linux/amd64`, plus a native `darwin/arm64`
  build on a macOS runner, uploaded as CI artifacts. Nothing is published.
- Pushing a `v*` tag builds the per-OS archives (`tar.gz` for macOS/Linux, `zip`
  for Windows) plus `checksums.txt` and publishes a GitHub Release. The tag and
  commit are stamped into the binary (`agentctl version`).
- macOS idle detection is a cgo call into Quartz, so `darwin/arm64` is built on a
  macOS runner with `CGO_ENABLED=1` rather than cross-built by GoReleaser, and its
  archive and checksum line are attached to the same release. A cgo-less macOS
  binary would carry the unsupported idle stub, and `agentctl run` refuses to
  start on one ([ADR 098](../docs/adr/098-go-idle-fail-closed.md)).

Now that `agentctl` has a daemon `run` mode, the deploy scripts can install the
prebuilt binary as the running agent. Download the archive for your OS from the
GitHub Release, extract `agentctl` (`agentctl.exe` on Windows), and pass its path
to the installer:

```bash
deploy/macos/install.sh --go-binary /path/to/agentctl            # macOS
```

```powershell
deploy\windows\install.ps1 -GoBinary C:\path\to\agentctl.exe     # Windows
```

The Go binary replaces only the Python venv. The agent still reads the same
`~/.fallow/agent.toml`, still supervises the same `llama-server` staged in §2,
and runs in the same user-session service (LaunchAgent / Scheduled Task). Without
the flag the installers build and install the Python agent exactly as before.
See [ADR 041](../docs/adr/041-go-agent-release.md) for the wiring decision.
