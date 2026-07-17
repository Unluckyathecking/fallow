"""Stub runners for the planned MoE benchmarks (ADR 076).

Each runner names one benchmark from the roadmap and raises `NotImplementedError`
with what it still needs. They are honest placeholders: none of them fakes a
result, and the distributed ones cannot be implemented without a real fleet and,
for the llama.cpp RPC baseline, an isolated off-network bench (the RPC backend is
an insecure proof of concept — see the module warning in README.md).

Fill one in only alongside the real model or fleet it measures; until then the
harness plumbing is exercised by the fake runner in the smoke test, not these.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness import BenchmarkConfig
from metrics import RunObservation


@dataclass(frozen=True)
class SingleMachineOffloadRunner:
    """Single-machine CPU/VRAM-offload baseline.

    The floor everything distributed is compared against: one machine paging
    experts between VRAM, RAM, and SSD. Needs a real GGUF model and a served
    endpoint to measure.
    """

    name: str = "single_machine_offload"

    def run(self, config: BenchmarkConfig) -> RunObservation:
        raise NotImplementedError(
            "single-machine offload baseline needs a real model and a served endpoint; "
            "wire it up next to the model, do not synthesise an observation"
        )


@dataclass(frozen=True)
class LlamaCppRpcRunner:
    """llama.cpp RPC distribution baseline.

    Reference point for expert/tensor splitting across machines. The llama.cpp
    RPC backend is a proof of concept that trusts its peers and does no
    authentication; it must run only on an isolated bench, never on the school
    network or the production fleet.
    """

    name: str = "llama_cpp_rpc"

    def run(self, config: BenchmarkConfig) -> RunObservation:
        raise NotImplementedError(
            "llama.cpp RPC baseline needs a real model split across an isolated, "
            "off-network bench; the RPC backend is insecure and must never touch the "
            "school network or the production fleet"
        )


@dataclass(frozen=True)
class ActivationCompressionRunner:
    """Activation-compression experiment.

    Measures how far cross-machine activation tensors can be quantised or
    compressed before end-to-end quality degrades. Needs a real distributed run
    to have activations worth compressing.
    """

    name: str = "activation_compression"

    def run(self, config: BenchmarkConfig) -> RunObservation:
        raise NotImplementedError(
            "activation-compression experiment needs a real distributed run and a "
            "quality reference; there are no activations to compress without a model and fleet"
        )


@dataclass(frozen=True)
class SpeculativeDecodingRunner:
    """Speculative-decoding experiment.

    A small on-machine drafter proposes tokens that the large distributed model
    verifies in one batched pass, to hide per-token network cost. Needs both a
    drafter and a distributed verifier.
    """

    name: str = "speculative_decoding"

    def run(self, config: BenchmarkConfig) -> RunObservation:
        raise NotImplementedError(
            "speculative-decoding experiment needs an on-machine drafter and a real "
            "distributed verifier to measure acceptance rate; do not fake a run"
        )
