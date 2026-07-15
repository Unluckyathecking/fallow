"""``EmbedWorker``: batch text embedding against a local llama-server replica.

Input is a JSON array of strings (chunks). The worker POSTs the whole batch to
the replica's OpenAI-compatible ``/v1/embeddings`` endpoint and emits a compact
JSON payload ``{embeddings, model_id, dims}``.
"""

import json
from typing import Any

import httpx

from fallow_agent.workers.config import HTTP_OK, EmbedConfig
from fallow_agent.workers.errors import WorkerBackendError, WorkerInputError
from fallow_agent.workers.types import EndpointResolver, LocalEndpoint, WorkOutput
from fallow_protocol.messages import WorkMetrics, WorkUnitLease


class EmbedWorker:
    """Embeds a batch of chunks via a local OpenAI-compatible replica."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        resolve_endpoint: EndpointResolver,
        config: EmbedConfig | None = None,
    ) -> None:
        self._client = client
        self._resolve_endpoint = resolve_endpoint
        self._config = config or EmbedConfig()

    async def run(self, lease: WorkUnitLease, input_bytes: bytes) -> WorkOutput:
        chunks = _parse_chunks(input_bytes)
        endpoint = self._resolve_endpoint(lease.model_id)
        data = await self._post(endpoint, lease.model_id, chunks)
        embeddings, tokens = _parse_embeddings(data, len(chunks))
        dims = len(embeddings[0]) if embeddings else 0
        payload = _encode_payload(embeddings, lease.model_id, dims)
        metrics = WorkMetrics(duration_s=0.0, items=len(chunks), tokens=tokens)
        return WorkOutput(payload=payload, metrics=metrics)

    async def _post(
        self, endpoint: LocalEndpoint, model_id: str, chunks: list[str]
    ) -> dict[str, Any]:
        url = f"{self._config.scheme}://{endpoint.host}:{endpoint.port}{self._config.path}"
        body = {"model": model_id, "input": chunks}
        try:
            response = await self._client.post(
                url, json=body, timeout=self._config.request_timeout_s
            )
        except httpx.HTTPError as exc:
            raise WorkerBackendError(f"embedding request failed: {exc}") from exc
        if response.status_code != HTTP_OK:
            raise WorkerBackendError(f"embedding replica returned HTTP {response.status_code}")
        return _decode_json_object(response)


def _decode_json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise WorkerBackendError(f"embedding response was not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkerBackendError("embedding response was not a JSON object")
    return data


def _parse_chunks(input_bytes: bytes) -> list[str]:
    try:
        parsed = json.loads(input_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WorkerInputError(f"embed input is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise WorkerInputError("embed input must be a JSON array of strings")
    if not parsed:
        raise WorkerInputError("embed input contains no chunks")
    return parsed


def _parse_embeddings(data: dict[str, Any], expected: int) -> tuple[list[list[float]], int | None]:
    raw = data.get("data")
    if not isinstance(raw, list):
        raise WorkerBackendError("embedding response missing 'data' array")
    if len(raw) != expected:
        raise WorkerBackendError(f"expected {expected} embeddings, got {len(raw)}")
    ordered = sorted(raw, key=_embedding_index)
    embeddings = [_embedding_vector(item) for item in ordered]
    return embeddings, _usage_tokens(data)


def _embedding_index(item: Any) -> int:
    if isinstance(item, dict):
        index = item.get("index", 0)
        if isinstance(index, int):
            return index
    return 0


def _embedding_vector(item: Any) -> list[float]:
    vector = item.get("embedding") if isinstance(item, dict) else None
    if not isinstance(vector, list) or not all(isinstance(x, (int, float)) for x in vector):
        raise WorkerBackendError("embedding entry missing a numeric 'embedding' vector")
    return [float(x) for x in vector]


def _usage_tokens(data: dict[str, Any]) -> int | None:
    usage = data.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, int):
            return total
    return None


def _encode_payload(embeddings: list[list[float]], model_id: str, dims: int) -> bytes:
    document = {"embeddings": embeddings, "model_id": model_id, "dims": dims}
    return json.dumps(document, separators=(",", ":")).encode("utf-8")
