"""Optional LAN Site Mode mDNS advertisement (ADR 090)."""

from .advertisement import (
    MAX_LABEL_LENGTH,
    MAX_SITE_ID_BYTES,
    SERVICE_TYPE,
    TXT_VERSION,
    AdvertiseError,
    Advertisement,
    SiteAdvertiser,
    build_advertisement,
)
from .mdns import ZeroconfAdvertiser

__all__ = [
    "MAX_LABEL_LENGTH",
    "MAX_SITE_ID_BYTES",
    "SERVICE_TYPE",
    "TXT_VERSION",
    "AdvertiseError",
    "Advertisement",
    "SiteAdvertiser",
    "ZeroconfAdvertiser",
    "build_advertisement",
]
