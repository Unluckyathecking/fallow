"""The python-zeroconf responder behind the Site Mode advertisement.

``zeroconf`` is the maintained cross-platform mDNS implementation and its
registration lifecycle is exactly the one this feature needs, so the coordinator
delegates to it rather than writing DNS packets. Nothing is constructed until
:meth:`ZeroconfAdvertiser.register` runs, so a coordinator with mDNS off opens no
multicast socket and starts no responder.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from zeroconf import Error as ZeroconfError
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from fallow_coordinator.discovery.advertisement import (
    SERVICE_TYPE,
    AdvertiseError,
    Advertisement,
)

# Opens the multicast sockets bound to the given interface addresses.
ZeroconfFactory = Callable[[Sequence[str]], AsyncZeroconf]


class ZeroconfAdvertiser:
    """Publish one Site Mode service record for the lifetime of the coordinator."""

    def __init__(self, open_zeroconf: ZeroconfFactory | None = None) -> None:
        self._open = open_zeroconf if open_zeroconf is not None else _open_zeroconf
        self._zeroconf: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None

    async def register(self, advertisement: Advertisement) -> None:
        if self._zeroconf is not None:
            raise AdvertiseError("site mDNS advertisement is already registered")
        info = ServiceInfo(
            SERVICE_TYPE,
            advertisement.instance_name,
            port=advertisement.port,
            properties=advertisement.txt,
            server=advertisement.server,
            parsed_addresses=list(advertisement.addresses),
        )
        zeroconf = self._open(advertisement.addresses)
        try:
            # A neighbour already holding the instance name is a label clash, not
            # a trust problem: the site id an agent checks lives in TXT, so let
            # zeroconf publish under "<label>-2" instead of failing startup.
            await zeroconf.async_register_service(info, allow_name_change=True)
        except (OSError, ZeroconfError) as exc:
            await zeroconf.async_close()
            raise AdvertiseError(f"site mDNS registration failed: {exc}") from exc
        self._zeroconf = zeroconf
        self._info = info

    async def unregister(self) -> None:
        zeroconf, info = self._zeroconf, self._info
        self._zeroconf, self._info = None, None
        if zeroconf is None or info is None:
            return
        try:
            await zeroconf.async_unregister_service(info)
        finally:
            await zeroconf.async_close()


def _open_zeroconf(interfaces: Sequence[str]) -> AsyncZeroconf:
    return AsyncZeroconf(interfaces=list(interfaces))
