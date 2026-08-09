from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Request

from fallow_coordinator.app.config import CoordinatorConfig
from fallow_coordinator.app.deps import authenticate_admin
from fallow_coordinator.site.models import JoinBundlesRequest, JoinBundleV1

TokenFactory = Callable[[], Awaitable[str]]


def _spki_pin(certfile: Path) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    cert = x509.load_pem_x509_certificate(certfile.read_bytes())
    der = cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return "sha256/" + base64.b64encode(hashlib.sha256(der).digest()).decode("ascii")


def build_site_admin_router(
    settings: CoordinatorConfig, create_site_token: TokenFactory
) -> APIRouter:
    router = APIRouter(prefix="/v1/admin/site")

    @router.post("/join-bundles", status_code=201)
    async def join_bundles(
        body: JoinBundlesRequest, request: Request
    ) -> dict[str, list[JoinBundleV1]]:
        await authenticate_admin(
            request.app.state.coordinator, request.headers.get("authorization")
        )
        site = settings.site
        assert site.tls_certfile is not None and site.site_id is not None
        pin = _spki_pin(site.tls_certfile)
        return {
            "bundles": [
                JoinBundleV1(
                    site_id=site.site_id,
                    coordinator_urls=site.public_urls,
                    coordinator_spki_sha256=(pin,),
                    enrollment_token=await create_site_token(),
                    mdns_service=site.mdns_service,
                )
                for _ in range(body.count)
            ]
        }

    return router
