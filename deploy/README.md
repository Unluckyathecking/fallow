# Fallow deployment (v0.1)

Scripts and service definitions that stage the `llama.cpp` binary and install the
Fallow **coordinator** and **agents** as long-running background processes.

> **Honesty note.** These scripts were authored in a sandbox with **no network
> access and no Windows/macOS service host**. Every step that downloads a file or
> talks to `launchd` / Task Scheduler is annotated **(untested — verify on
> target)** in the script itself. Treat this directory as a reviewed starting
> point, not a green-tested installer. Verify the pinned llama.cpp tag and asset
> names against <https://github.com/ggml-org/llama.cpp/releases> before first use.

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

## 0. Support matrix

| Role            | macOS (Apple Silicon) | Windows x64 (CUDA)      | Linux           |
| --------------- | --------------------- | ----------------------- | --------------- |
| **Coordinator** | ✅ supported          | — (not targeted in v0.1) | ✅ supported     |
| **Agent**       | ✅ supported          | ✅ supported             | — (v0.2)        |

- **Coordinator** is a plain long-running process (`fallow_coordinator.app` +
  `uvicorn`). It has no idle/GUI-session constraint, so it runs equally well on
  macOS or Linux (systemd unit is out of scope for this module — run it under
  your process manager of choice, or `launchd` on a Mac using the same pattern as
  the agent plist).
- **Agents** must run **inside the logged-in user's GUI session** on both
  macOS and Windows — see the "why user session" boxes below. That is the whole
  reason this module exists rather than shipping a system service.

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

Downloads **two** archives for the pinned tag and unpacks both into
`deploy\bin\windows\`:

1. `llama-…-bin-win-cuda-cu12.4-x64.zip` — the CUDA build, **and**
2. `cudart-llama-bin-win-cu12.4-x64.zip` — the CUDA runtime DLLs.

> ⚠️ **The classic trap.** The `win-cuda` archive does **not** contain
> `cudart64_*.dll` / `cublas64_*.dll`. If you unpack only the first zip,
> `llama-server.exe` dies at launch with a missing-DLL error. You **must** unpack
> the matching `cudart-…` zip into the same folder. The CUDA sub-version of the
> two archives must match (both `cu12.4`). `fetch-llama.ps1` fetches and unpacks
> both for you.

### Pinning & the lockfile

The release tag lives in **one variable** at the top of each fetch script
(`LLAMA_RELEASE`). Bump it there (and, on Windows, the matching `CudaTag`) to
move builds. Because llama.cpp publishes no per-asset checksum file, the scripts
**record** the downloaded SHA256 into `deploy/llama-version.lock` on first run and
**verify against it** on later runs — commit `llama-version.lock` so every machine
pins the identical bytes.

---

## 3. Coordinator

Run the coordinator on a machine that stays up (a Mac mini or a Linux box). It
serves the admin API and the OpenAI-compatible gateway. Minimal manual form:

```bash
cd <fallow checkout>
uv sync --no-dev
.venv/bin/python -m uvicorn fallow_coordinator.app:build_app --factory \
    --host <tailnet-ip> --port 8080
```

Configure it from `deploy/coordinator.example.toml` (provided by the config
module) copied to `~/.fallow/coordinator.toml`. Managing the coordinator as a
`launchd`/systemd service follows the same pattern as the agent plist below and is
left to the operator in v0.1.

### 3.1 Model pre-staging (zero-egress labs)

For air-gapped / zero-egress offices, stage models **once on the coordinator**;
agents then pull blobs **from the coordinator**, never from the public internet:

```bash
# On the coordinator host: download + register a model blob.
flw models pull <source-url> \
    --model-id qwen2.5-7b-instruct-q4 \
    --family qwen2.5 --quant Q4_K_M --worker-kind chat
```

`flw models pull` streams the blob into the coordinator's `~/.fallow/blobs` and
registers its manifest. When a model is assigned to an agent, the agent's model
cache pulls the blob **from the coordinator's blob endpoint** over the tailnet, so
the only machine that needs egress is the coordinator (and even that can be primed
off a USB drive by dropping files into `~/.fallow/blobs` and registering with the
local path).

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
deploy\windows\fetch-llama.ps1     # once, stages llama-server.exe + cudart
deploy\windows\install.ps1         # bootstraps python, installs the task
```

`install.ps1`:

1. `uv python install 3.12` then `uv sync --no-dev` → `.venv`.
2. Copies `deploy\agent.example.toml` → `%USERPROFILE%\.fallow\agent.toml` if
   absent (edit the same fields as macOS; point `llama_binary` at
   `deploy\bin\windows\llama-server.exe`).
3. Renders `fallow-agent-task.xml` and registers it as an **at-logon Scheduled
   Task** running `pythonw -m fallow_agent run` in the user session.

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
  allow rule for `llama-server.exe` pinned to the SHA256 in
  `deploy/llama-version.lock` over blanket path exclusions.
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

---

## 6. Files in this directory

| Path                              | Purpose                                                        |
| --------------------------------- | ------------------------------------------------------------- |
| `fetch-llama.sh`                  | macOS: fetch + unpack pinned llama.cpp `macos-arm64`.         |
| `windows/fetch-llama.ps1`         | Windows: fetch + unpack pinned CUDA build **and** cudart.     |
| `macos/install.sh`                | Install agent as a `launchd` LaunchAgent (user session).      |
| `macos/uninstall.sh`              | Remove the LaunchAgent (`--purge` to delete `~/.fallow`).     |
| `macos/com.fallow.agent.plist`    | LaunchAgent template (tokens filled by `install.sh`).         |
| `windows/install.ps1`             | Install agent as an at-logon Scheduled Task (user session).   |
| `windows/uninstall.ps1`           | Remove the task (`-Purge` to delete `~\.fallow`).            |
| `windows/fallow-agent-task.xml`   | Task Scheduler template (tokens filled by `install.ps1`).     |
| `agent.example.toml`              | Example agent config (provided by the config module).         |
| `coordinator.example.toml`        | Example coordinator config (provided by the config module).   |
| `llama-version.lock`              | Generated on first fetch; pins asset SHA256s — commit it.     |
| `bin/<platform>/`                 | Fetched llama.cpp binaries (git-ignored, per-host).           |
