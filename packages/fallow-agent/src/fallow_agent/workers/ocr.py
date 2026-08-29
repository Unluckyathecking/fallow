"""``OcrWorker``: one page image → Markdown/LaTeX via a local vision replica.

Input is the chunker's self-contained JSON unit ``{schema, prompt_version,
image_b64}``. The worker POSTs the page to the replica's OpenAI-compatible
``/v1/chat/completions`` endpoint with the versioned transcription prompt and
emits ``{schema, model_id, prompt_version, markdown, confidence, warnings}``.

Quality problems (empty or truncated output) are recorded as warnings on a
SUCCEEDED unit — retrying will not fix them and must not burn the unit's
attempts. Transport/replica failures raise, so the lease machinery retries.
"""

import base64
import binascii
import json
import math
from typing import Any

import httpx

from fallow_agent.workers.config import HTTP_OK, OcrConfig
from fallow_agent.workers.errors import WorkerBackendError, WorkerInputError
from fallow_agent.workers.types import EndpointResolver, LocalEndpoint, WorkOutput
from fallow_protocol.messages import WorkMetrics, WorkUnitLease

_UNIT_SCHEMA = "ocr-unit/1"
_RESULT_SCHEMA = "ocr-result/1"

# Versioned prompts, keyed by the version the coordinator's chunker stamped into
# the unit (``fallow_coordinator.app.chunker.OCR_PROMPT_VERSION``). A unit
# carrying a version this agent does not know fails rather than guessing.
_PROMPTS = {
    1: (
        "Transcribe this page exactly, as Markdown. Write every mathematical "
        "expression as LaTeX between $ or $$ delimiters. Preserve question "
        "numbering, tables, and reading order. Output only the transcription."
    ),
}

_JPEG_MAGIC = b"\xff\xd8"
_WEBP_TAG = b"WEBP"


class OcrWorker:
    """OCRs one page image via a local OpenAI-compatible vision replica."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        resolve_endpoint: EndpointResolver,
        config: OcrConfig | None = None,
    ) -> None:
        self._client = client
        self._resolve_endpoint = resolve_endpoint
        self._config = config or OcrConfig()

    async def run(self, lease: WorkUnitLease, input_bytes: bytes) -> WorkOutput:
        prompt_version, image = _parse_unit(input_bytes)
        endpoint = self._resolve_endpoint(lease.model_id)
        data = await self._post(endpoint, lease.model_id, prompt_version, image)
        markdown, confidence, warnings = _parse_completion(data)
        payload = _encode_payload(lease.model_id, prompt_version, markdown, confidence, warnings)
        metrics = WorkMetrics(duration_s=0.0, items=1)
        return WorkOutput(payload=payload, metrics=metrics)

    async def _post(
        self, endpoint: LocalEndpoint, model_id: str, prompt_version: int, image: bytes
    ) -> dict[str, Any]:
        url = f"{self._config.scheme}://{endpoint.host}:{endpoint.port}{self._config.path}"
        data_url = f"data:{_mime(image)};base64,{base64.b64encode(image).decode('ascii')}"
        body = {
            "model": model_id,
            "temperature": 0,
            "max_tokens": self._config.max_output_tokens,
            "logprobs": True,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPTS[prompt_version]},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        }
        try:
            response = await self._client.post(
                url, json=body, timeout=self._config.request_timeout_s
            )
        except httpx.HTTPError as exc:
            raise WorkerBackendError(f"ocr request failed: {exc}") from exc
        if response.status_code != HTTP_OK:
            raise WorkerBackendError(f"ocr replica returned HTTP {response.status_code}")
        return _decode_json_object(response)


def _mime(image: bytes) -> str:
    if image.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if image[8:12] == _WEBP_TAG:
        return "image/webp"
    return "image/png"


def _parse_unit(input_bytes: bytes) -> tuple[int, bytes]:
    try:
        document = json.loads(input_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WorkerInputError(f"ocr input is not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema") != _UNIT_SCHEMA:
        raise WorkerInputError(f"ocr input schema must be {_UNIT_SCHEMA!r}")
    version = document.get("prompt_version")
    if version not in _PROMPTS:
        raise WorkerInputError(f"unknown ocr prompt version: {version!r}")
    encoded = document.get("image_b64")
    if not isinstance(encoded, str):
        raise WorkerInputError("ocr input is missing 'image_b64'")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WorkerInputError(f"ocr input image_b64 is not valid base64: {exc}") from exc
    if not image:
        raise WorkerInputError("ocr input contains no image bytes")
    return version, image


def _decode_json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise WorkerBackendError(f"ocr response was not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkerBackendError("ocr response was not a JSON object")
    return data


def _parse_completion(data: dict[str, Any]) -> tuple[str, float | None, list[str]]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise WorkerBackendError("ocr response missing 'choices'")
    choice = choices[0]
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise WorkerBackendError("ocr response missing message content")
    warnings: list[str] = []
    if not content.strip():
        warnings.append("empty-output")
    if choice.get("finish_reason") == "length":
        warnings.append("truncated")
    return content, _confidence(choice.get("logprobs")), warnings


def _confidence(logprobs: Any) -> float | None:
    """Geometric-mean token probability, or None when the replica gave none."""
    entries = logprobs.get("content") if isinstance(logprobs, dict) else None
    if not isinstance(entries, list) or not entries:
        return None
    values = [
        entry["logprob"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("logprob"), (int, float))
    ]
    if len(values) != len(entries):
        return None
    return math.exp(sum(values) / len(values))


def _encode_payload(
    model_id: str, prompt_version: int, markdown: str, confidence: float | None, warnings: list[str]
) -> bytes:
    document = {
        "schema": _RESULT_SCHEMA,
        "model_id": model_id,
        "prompt_version": prompt_version,
        "markdown": markdown,
        "confidence": confidence,
        "warnings": warnings,
    }
    return json.dumps(document, separators=(",", ":")).encode("utf-8")
