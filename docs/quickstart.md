# Quickstart: serve one chat request

This guide starts one coordinator and one Python agent, registers a local GGUF
file, and sends a request through Fallow's OpenAI-compatible chat endpoint.
Fallow is pre-alpha, so use a test machine and test data.

The commands below assume that the coordinator and agent each have a clone of
this repository. Run commands from the repository root unless a step says
otherwise.

## 1. Prepare both machines

Install Python 3.12 or 3.13, [uv](https://docs.astral.sh/uv/), and Git. On both
machines, clone the repository and install the workspace:

```bash
git clone https://github.com/Unluckyathecking/fallow.git
cd fallow
uv sync --frozen
```

The agent also needs:

- a `llama-server` binary from llama.cpp;
- enough RAM or VRAM for the model;
- a small chat GGUF file;
- network access to the coordinator; and
- an address that the coordinator can use to reach its replica ports.

Tailscale is the intended transport. Bind the coordinator and agent to their
exact tailnet addresses. Do not use a wildcard such as `0.0.0.0`: the
`llama-server` replica has no authentication of its own.

Allow the agent to reach TCP port `8330` on the coordinator. Allow the
coordinator to reach the agent's configured replica range, `8100` through
`8115` in this example. Apply those rules in the tailnet policy as well as any
host firewall.

The repository includes fetch scripts for the supported packaged targets:

```bash
# Apple Silicon macOS
./deploy/fetch-llama.sh
```

From Windows PowerShell:

```powershell
.\deploy\windows\fetch-llama.ps1
```

Read the pin and platform notes at the top of the relevant script before using
it. The macOS script targets Apple Silicon. The Windows script targets x64 with
CUDA. For Linux or another target, install a compatible `llama-server` build
from llama.cpp. In every case, note the final absolute path to the executable.

Put the GGUF file on the coordinator host. The `flw models register` command
currently records a coordinator-local path, so run that command on the
coordinator.

## 2. Start the coordinator

Create `coordinator.toml` on the coordinator host. Replace `100.64.0.10` with
that host's tailnet address and replace the admin key with a random value. The
relative paths below are resolved from the directory where you start the
coordinator.

```toml
db_path = ".fallow-run/coordinator.db"
blob_dir = ".fallow-run/blobs"
unit_input_dir = ".fallow-run/units"
result_dir = ".fallow-run/results"
events_jsonl_path = ".fallow-run/events.jsonl"
gateway_log_path = ".fallow-run/gateway.jsonl"

admin_key = "replace-with-a-random-admin-key"
host = "100.64.0.10"
port = 8330
```

Start the service:

```bash
uv run python -m fallow_coordinator serve --config coordinator.toml
```

Leave it running. The rest of the coordinator-side commands need its URL and
admin key. Set them in a second terminal:

```bash
export FLW_COORDINATOR_URL="http://100.64.0.10:8330"
export FLW_ADMIN_KEY="replace-with-a-random-admin-key"
```

You can store those two values in `~/.fallow/cli.toml` instead. Restrict that
file to your user because it contains the admin key:

```toml
coordinator_url = "http://100.64.0.10:8330"
admin_key = "replace-with-a-random-admin-key"
```

## 3. Register the model

Choose a stable ID for the model. The example uses a small Qwen chat model, but
the file path must point to the GGUF that you placed on this coordinator.

```bash
uv run flw models register \
  --file /absolute/path/to/qwen2.5-0.5b-instruct-q4_k_m.gguf \
  --model-id qwen2.5-0.5b-instruct \
  --family qwen2.5 \
  --quant Q4_K_M \
  --worker-kind chat
```

The command hashes the file, registers its manifest, and prints:

```text
registered: qwen2.5-0.5b-instruct
```

The file must remain readable at the registered path. The coordinator serves
it to assigned agents from there.

## 4. Enroll and start the agent

On the coordinator, mint a one-time enrollment token:

```bash
uv run flw enroll new-token
```

Copy the value after `enrollment_token:`. On the agent host, create
`agent.toml`. Replace the coordinator URL, token, agent tailnet address, and
`llama-server` path. Use absolute state paths if you will not always start the
agent from the same directory.

```toml
coordinator_url = "http://100.64.0.10:8330"
enrollment_token = "paste-the-one-time-token-here"
bind_host = "100.64.0.11"
llama_server_binary = "/absolute/path/to/llama-server"

state_path = ".fallow-run/agent-state.json"
cache_dir = ".fallow-run/models"
events_jsonl_path = ".fallow-run/events.jsonl"
results_dir = ".fallow-run/results"

[port_range]
start = 8100
count = 16
```

Start the agent and leave it running:

```bash
uv run python -m fallow_agent run --config agent.toml
```

The first run exchanges the enrollment token for an agent identity and stores
that identity at `state_path`. Later runs reuse it, so the enrollment token is
not needed after the first successful start.

Back on the coordinator, find the new agent ID:

```bash
uv run flw agents list
```

## 5. Assign the model

Use the ID from the agents table:

```bash
uv run flw assign qwen2.5-0.5b-instruct AGENT_ID
```

The agent receives the assignment on a heartbeat, downloads and verifies the
GGUF, then starts `llama-server` on a port from its configured range. Fallow
only serves on an idle machine. A desktop normally becomes eligible after 120
seconds without user input, so move away from the agent host while testing.
Headless Linux agents report as idle.

Check the fleet while the model starts:

```bash
uv run flw status
uv run flw agents list
uv run flw models list
```

Wait until the agent is idle and the agents table shows the model. A request
sent before a healthy replica is ready returns `503` with the error type
`no_replica_available`.

## 6. Create a client key

Create a key restricted to this model:

```bash
uv run flw keys new quickstart --allow qwen2.5-0.5b-instruct
```

Copy the value after `api_key:`. Client keys are only shown once.

## 7. Send a chat request

Set the client key in the shell that will call the gateway:

```bash
export FALLOW_CLIENT_KEY="paste-the-client-key-here"
```

Send a request to the coordinator:

```bash
curl -sS "http://100.64.0.10:8330/v1/chat/completions" \
  -H "Authorization: Bearer ${FALLOW_CLIENT_KEY}" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "model": "qwen2.5-0.5b-instruct",
    "messages": [
      {"role": "user", "content": "Reply with one short sentence about the moon."}
    ]
  }'
```

The response body comes from the installed `llama-server` build. Its generated
text is in `choices[0].message.content`. An abridged response looks like this:

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "qwen2.5-0.5b-instruct",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The moon is Earth's natural satellite."
      },
      "finish_reason": "stop"
    }
  ]
}
```

If the request does not reach a replica, check `flw agents list` first. The
usual causes are an active agent, an assignment that has not reconciled yet, a
model that does not fit the machine, or blocked tailnet access to the agent's
configured port range.
