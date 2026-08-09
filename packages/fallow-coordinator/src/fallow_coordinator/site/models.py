from typing import Literal

from pydantic import Field

from fallow_protocol.base import FallowModel


class JoinBundlesRequest(FallowModel):
    count: int = Field(ge=1, le=16)


class JoinBundleV1(FallowModel):
    version: Literal[1] = 1
    site_id: str = Field(min_length=1)
    coordinator_urls: tuple[str, ...] = Field(min_length=1)
    coordinator_spki_sha256: tuple[str, ...] = Field(min_length=1)
    enrollment_token: str = Field(min_length=1)
    mdns_service: Literal["_fallow._tcp.local."] | None = None
