"""Command construction seam for launching inference replicas.

`CommandFactory` is the injection point: `start_replica` calls it to turn a
manifest + resolved model path + port into an argv list. The real
implementation for llama.cpp is `LlamaServerCommandFactory`; tests inject a
trivial factory that spawns a harmless sleeper.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fallow_agent.supervisor.config import SupervisorConfig
from fallow_protocol.models import ModelManifest

GPU_LAYERS_FLAG = "-ngl"
FLASH_ATTN_FLAG = "--flash-attn"


class CommandFactory(Protocol):
    """Builds the argv used to launch one replica (no shell)."""

    def __call__(self, manifest: ModelManifest, model_path: Path, port: int) -> list[str]: ...


@dataclass(frozen=True)
class LlamaServerCommandFactory:
    """Real llama-server command builder bound to a `SupervisorConfig`."""

    config: SupervisorConfig

    def __call__(self, manifest: ModelManifest, model_path: Path, port: int) -> list[str]:
        cmd = [
            str(self.config.llama_binary),
            "-m",
            str(model_path),
            "--port",
            str(port),
            "--host",
            self.config.bind_host,
            "--parallel",
            str(self.config.parallel),
            "-c",
            str(self.config.context_size),
            *manifest.default_args,
        ]
        if manifest.min_vram_mb > 0:
            cmd.extend([GPU_LAYERS_FLAG, str(self.config.gpu_layers), FLASH_ATTN_FLAG])
        return cmd


def llama_server_command(config: SupervisorConfig) -> CommandFactory:
    """Return the real llama-server `CommandFactory` for `config`."""
    return LlamaServerCommandFactory(config)
