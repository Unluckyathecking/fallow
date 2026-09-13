"""Constants and immutable per-worker tuning.

No magic numbers live in the logic modules: every tunable is a named constant
here, and per-worker behaviour is captured in a frozen config model.
"""

from pydantic import BaseModel, ConfigDict, Field

# Local replicas are OpenAI-compatible llama-server / faster-whisper endpoints
# reached over plain HTTP on the loopback / tailnet interface.
HTTP_SCHEME = "http"
EMBEDDINGS_PATH = "/v1/embeddings"
HTTP_OK = 200

# A batch embedding call can carry thousands of chunks; give it a generous cap.
_DEFAULT_REQUEST_TIMEOUT_S = 300.0

_DEFAULT_WHISPER_DEVICE = "cpu"
_DEFAULT_WHISPER_COMPUTE = "int8"
_DEFAULT_BEAM_SIZE = 5
_DEFAULT_AUDIO_SUFFIX = ".wav"


class EmbedConfig(BaseModel):
    """Immutable knobs for one :class:`EmbedWorker`."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    scheme: str = HTTP_SCHEME
    path: str = EMBEDDINGS_PATH
    request_timeout_s: float = Field(default=_DEFAULT_REQUEST_TIMEOUT_S, gt=0)


class TranscribeConfig(BaseModel):
    """Immutable knobs for one :class:`TranscribeWorker`.

    ``model_size_or_path`` is a faster-whisper model size (``"base"``,
    ``"large-v3"``) or a local model directory; it is required so the worker is
    never launched without an explicit model choice.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model_size_or_path: str
    device: str = _DEFAULT_WHISPER_DEVICE
    compute_type: str = _DEFAULT_WHISPER_COMPUTE
    beam_size: int = Field(default=_DEFAULT_BEAM_SIZE, ge=1)
    audio_suffix: str = _DEFAULT_AUDIO_SUFFIX
