"""FastAPI router serving model blobs and manifests to authenticated agents.

Agents pull the blob (with resumable ``Range`` requests) and read the manifest
using their device token; both endpoints 404 on unknown/disabled models.
"""

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from fallow_coordinator.httpauth import authenticate_agent
from fallow_coordinator.modelserve.blob import (
    OCTET_STREAM,
    RangeNotSatisfiable,
    parse_range,
    stream_file,
)
from fallow_coordinator.modelserve.protocols import BlobRegistry

_UNKNOWN_MODEL = "unknown or disabled model"


async def _blob_size(path: str) -> int:
    target = anyio.Path(path)
    if not await target.is_file():
        raise HTTPException(status_code=404, detail=_UNKNOWN_MODEL)
    stat = await target.stat()
    return stat.st_size


def create_modelserve_router(registry: BlobRegistry) -> APIRouter:
    """Build the model-serving router bound to ``registry``."""
    router = APIRouter()

    async def require_agent(authorization: str | None = Header(default=None)) -> str:
        return await authenticate_agent(registry, authorization)

    @router.get("/v1/models/{model_id}/manifest")
    async def get_manifest(model_id: str, _agent_id: str = Depends(require_agent)) -> Response:
        manifest = await registry.get_manifest(model_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail=_UNKNOWN_MODEL)
        return Response(content=manifest.model_dump_json(), media_type="application/json")

    @router.get("/v1/models/{model_id}/blob")
    async def get_blob(
        model_id: str, request: Request, _agent_id: str = Depends(require_agent)
    ) -> StreamingResponse:
        record = await registry.get_model(model_id)
        if record is None or not record.enabled:
            raise HTTPException(status_code=404, detail=_UNKNOWN_MODEL)
        size = await _blob_size(record.blob_path)
        try:
            byte_range = parse_range(request.headers.get("range"), size)
        except RangeNotSatisfiable as exc:
            raise HTTPException(
                status_code=416,
                detail="requested range not satisfiable",
                headers={"Content-Range": f"bytes */{size}"},
            ) from exc
        if byte_range is None:
            return _full_response(record.blob_path, size)
        return _partial_response(record.blob_path, size, byte_range.start, byte_range.end)

    return router


def _full_response(path: str, size: int) -> StreamingResponse:
    headers = {"Content-Length": str(size), "Accept-Ranges": "bytes"}
    return StreamingResponse(
        stream_file(path, 0, size),
        status_code=200,
        media_type=OCTET_STREAM,
        headers=headers,
    )


def _partial_response(path: str, size: int, start: int, end: int) -> StreamingResponse:
    length = end - start + 1
    headers = {
        "Content-Length": str(length),
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
    }
    return StreamingResponse(
        stream_file(path, start, length),
        status_code=206,
        media_type=OCTET_STREAM,
        headers=headers,
    )
