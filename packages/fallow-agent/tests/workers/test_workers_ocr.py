"""Unit tests for OcrWorker against an httpx.MockTransport replica."""

import base64
import json
import math

import httpx
import pytest
from workers_helpers import make_lease

from fallow_agent.workers import LocalEndpoint, OcrWorker
from fallow_agent.workers.errors import (
    TransientWorkerError,
    WorkerBackendError,
    WorkerInputError,
)
from fallow_protocol.capabilities import WorkerKind

_PNG = b"\x89PNG\r\n\x1a\n-fake-page-bytes"
_JPEG = b"\xff\xd8\xff\xe0-fake-page-bytes"


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def _endpoint(_model_id: str) -> LocalEndpoint:
    return LocalEndpoint(host="127.0.0.1", port=8082)


def _lease():
    return make_lease(kind=WorkerKind.OCR, model_id="vlm-1")


def _unit(
    image: bytes = _PNG,
    *,
    prompt_version: int = 1,
    schema: str = "ocr-unit/1",
    page: str = "ab12cd34ef56-p00000.png",
) -> bytes:
    document = {
        "schema": schema,
        "prompt_version": prompt_version,
        "page": page,
        "image_b64": base64.b64encode(image).decode("ascii"),
    }
    return json.dumps(document).encode("utf-8")


def _completion(
    content: str = "# Q1\n\nSolve $x^2 = 2$.",
    *,
    finish_reason: str = "stop",
    logprobs: object = {"content": [{"logprob": -0.1}, {"logprob": -0.3}]},
) -> dict:
    choice: dict = {"message": {"content": content}, "finish_reason": finish_reason}
    if logprobs is not None:
        choice["logprobs"] = logprobs
    return {"choices": [choice]}


async def test_ocr_request_shape_and_payload() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["host"] = request.url.host
        seen["port"] = request.url.port
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    async with _client(httpx.MockTransport(handler)) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        output = await worker.run(_lease(), _unit())

    assert seen["path"] == "/v1/chat/completions"
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 8082

    body = seen["body"]
    assert body["model"] == "vlm-1"
    assert body["temperature"] == 0
    assert body["logprobs"] is True
    assert isinstance(body["max_tokens"], int) and body["max_tokens"] > 0
    (message,) = body["messages"]
    text_part, image_part = message["content"]
    assert text_part["type"] == "text" and text_part["text"].strip()
    url = image_part["image_url"]["url"]
    prefix = "data:image/png;base64,"
    assert url.startswith(prefix)
    assert base64.b64decode(url.removeprefix(prefix)) == _PNG

    payload = json.loads(output.payload)
    assert payload == {
        "schema": "ocr-result/1",
        "model_id": "vlm-1",
        "prompt_version": 1,
        # Echoed from the unit: idx order is the chunker's sorted-hash-name
        # order, so this is the only key that joins a result to corpus.json.
        "page": "ab12cd34ef56-p00000.png",
        "markdown": "# Q1\n\nSolve $x^2 = 2$.",
        "confidence": pytest.approx(math.exp(-0.2)),
        "warnings": [],
    }
    assert output.metrics.items == 1
    assert output.metrics.duration_s == 0.0


async def test_ocr_jpeg_input_gets_jpeg_data_url() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    async with _client(httpx.MockTransport(handler)) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        await worker.run(_lease(), _unit(_JPEG))

    (message,) = seen["body"]["messages"]
    url = message["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")


async def test_ocr_missing_logprobs_yields_null_confidence() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(logprobs=None))

    async with _client(httpx.MockTransport(handler)) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        output = await worker.run(_lease(), _unit())

    assert json.loads(output.payload)["confidence"] is None


async def test_ocr_empty_output_is_a_warning_not_a_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("   "))

    async with _client(httpx.MockTransport(handler)) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        output = await worker.run(_lease(), _unit())

    assert "empty-output" in json.loads(output.payload)["warnings"]


async def test_ocr_truncated_output_is_a_warning() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(finish_reason="length"))

    async with _client(httpx.MockTransport(handler)) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        output = await worker.run(_lease(), _unit())

    assert "truncated" in json.loads(output.payload)["warnings"]


async def test_ocr_rejects_non_json_input() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(WorkerInputError):
            await worker.run(_lease(), b"not json")


async def test_ocr_rejects_unknown_schema() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(WorkerInputError):
            await worker.run(_lease(), _unit(schema="ocr-unit/999"))


async def test_ocr_rejects_unknown_prompt_version() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(WorkerInputError):
            await worker.run(_lease(), _unit(prompt_version=999))


async def test_ocr_rejects_unit_without_page() -> None:
    document = {
        "schema": "ocr-unit/1",
        "prompt_version": 1,
        "image_b64": base64.b64encode(_PNG).decode("ascii"),
    }
    async with _client(httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(WorkerInputError):
            await worker.run(_lease(), json.dumps(document).encode())


async def test_ocr_rejects_undecodable_image() -> None:
    document = {
        "schema": "ocr-unit/1",
        "prompt_version": 1,
        "page": "ab12cd34ef56-p00000.png",
        "image_b64": "@@not-base64@@",
    }
    async with _client(httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(WorkerInputError):
            await worker.run(_lease(), json.dumps(document).encode())


async def test_ocr_5xx_is_transient_so_the_lease_can_requeue() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(503, text="unavailable"))
    async with _client(transport) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(TransientWorkerError):
            await worker.run(_lease(), _unit())


async def test_ocr_transport_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with _client(httpx.MockTransport(handler)) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(TransientWorkerError):
            await worker.run(_lease(), _unit())


async def test_ocr_4xx_is_a_permanent_backend_error() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(404, text="no such model"))
    async with _client(transport) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(WorkerBackendError):
            await worker.run(_lease(), _unit())


async def test_ocr_malformed_completion_is_backend_error() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": 1}))
    async with _client(transport) as client:
        worker = OcrWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(WorkerBackendError):
            await worker.run(_lease(), _unit())
