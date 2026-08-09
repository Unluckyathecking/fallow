"""What the coordinator publishes on the LAN, and the seam that publishes it.

The record is an address hint and nothing more. TXT carries the record version
and the site id; SRV/A/AAAA carry the configured HTTPS address and port. An agent
still checks the join file's site id and SPKI pin before it trusts a coordinator
it reached this way, so nothing here may ever carry a pin, token, model name or
credential.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Protocol

SERVICE_TYPE = "_fallow._tcp.local."

# TXT schema version. Bumped only when the key set changes, never per release.
TXT_VERSION = "1"

# A DNS label holds at most 63 bytes; the sanitised site id is ASCII, so
# character count and byte count are the same here.
MAX_LABEL_LENGTH = 63

# One TXT entry holds at most 255 bytes and the "site_id=" key spends 8 of them.
# The config caps site_id at 128 *characters*, which a multibyte id can carry
# well past this budget, so the limit is checked in bytes.
MAX_SITE_ID_BYTES = 255 - len("site_id=")

_UNSAFE_IN_LABEL = re.compile(r"[^A-Za-z0-9-]")


class AdvertiseError(Exception):
    """The configured advertisement cannot be published as specified."""


@dataclass(frozen=True)
class Advertisement:
    """One resolved Site Mode service record, ready to hand to a responder."""

    site_id: str
    label: str
    addresses: tuple[str, ...]
    port: int
    interface: str

    @property
    def instance_name(self) -> str:
        """The fully qualified DNS-SD service instance name."""
        return f"{self.label}.{SERVICE_TYPE}"

    @property
    def server(self) -> str:
        """The SRV target the A/AAAA records answer for."""
        return f"{self.label}.local."

    @property
    def txt(self) -> dict[str, str]:
        """The complete TXT payload. Two keys, both public."""
        return {"version": TXT_VERSION, "site_id": self.site_id}


class SiteAdvertiser(Protocol):
    """Publish and withdraw one advertisement. Faked in tests, zeroconf in prod."""

    async def register(self, advertisement: Advertisement) -> None: ...

    async def unregister(self) -> None: ...


def build_advertisement(*, site_id: str, host: str, port: int) -> Advertisement:
    """Resolve the configured bind into a publishable record.

    Raises :class:`AdvertiseError` when the interface cannot be advertised, so a
    misconfigured coordinator fails at startup rather than serving a Site Mode
    listener nobody can find.
    """
    encoded = len(site_id.encode("utf-8"))
    if encoded > MAX_SITE_ID_BYTES:
        raise AdvertiseError(
            f"site id is {encoded} bytes of UTF-8; an mDNS TXT entry holds at most "
            f"{MAX_SITE_ID_BYTES} bytes of site id"
        )
    return Advertisement(
        site_id=site_id,
        label=_label_for(site_id),
        addresses=_interface_addresses(host),
        port=port,
        interface=host,
    )


def _label_for(site_id: str) -> str:
    """Fold a site id into a single DNS label.

    The label is only a display name: the authoritative site id travels in TXT,
    and two sites whose ids fold together are separated by the responder's own
    name-collision handling.
    """
    label = _UNSAFE_IN_LABEL.sub("-", site_id).strip("-")[:MAX_LABEL_LENGTH].strip("-")
    if not label:
        raise AdvertiseError(f"site id {site_id!r} contains no character usable in an mDNS name")
    return label


def _interface_addresses(host: str) -> tuple[str, ...]:
    """The literal addresses to publish for the configured site interface.

    One address per family at most. A host that resolves to several addresses in
    the same family is ambiguous: the coordinator listens on one of them and
    publishing all would advertise addresses it does not serve.
    """
    try:
        resolved = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise AdvertiseError(f"site mDNS interface {host!r} does not resolve") from exc
    by_family: dict[int, list[str]] = {}
    for entry in resolved:
        address = ipaddress.ip_address(str(entry[4][0]).partition("%")[0])
        if address.is_unspecified:
            raise AdvertiseError(f"site mDNS interface {host!r} is a wildcard address")
        family = by_family.setdefault(address.version, [])
        if str(address) not in family:
            family.append(str(address))
    if not by_family:
        raise AdvertiseError(f"site mDNS interface {host!r} does not resolve")
    ambiguous = [addr for family in by_family.values() if len(family) > 1 for addr in family]
    if ambiguous:
        raise AdvertiseError(
            f"site mDNS interface {host!r} is ambiguous: it resolves to {', '.join(ambiguous)}"
        )
    return tuple(by_family[version][0] for version in sorted(by_family))
