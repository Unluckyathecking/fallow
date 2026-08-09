"""The zeroconf-backed advertiser, driven against a fake responder.

The real ``AsyncZeroconf`` is replaced by an injected factory, so these tests open
no multicast socket and touch no network interface.
"""

from __future__ import annotations

import pytest

from fallow_coordinator.discovery import (
    SERVICE_TYPE,
    AdvertiseError,
    ZeroconfAdvertiser,
    build_advertisement,
)


class FakeZeroconf:
    """Records what the advertiser asked of it; renames like a real responder."""

    def __init__(self, interfaces, *, taken=(), fail=None):
        self.interfaces = tuple(interfaces)
        self.taken = set(taken)
        self.fail = fail
        self.registered = []
        self.unregistered = []
        self.allow_name_change = None
        self.closed = 0

    async def async_register_service(self, info, allow_name_change=False, **_kwargs):
        self.allow_name_change = allow_name_change
        if self.fail is not None:
            raise self.fail
        if info.name in self.taken:
            if not allow_name_change:
                raise AssertionError("responder would have refused the duplicate name")
            label, _, rest = info.name.partition(".")
            info.name = f"{label}-2.{rest}"
        self.registered.append(info)

    async def async_unregister_service(self, info):
        self.unregistered.append(info)

    async def async_close(self):
        self.closed += 1


def advertisement(site_id="school-1"):
    return build_advertisement(site_id=site_id, host="127.0.0.1", port=8443)


def opener(**kwargs):
    """A factory that builds one FakeZeroconf and keeps it reachable."""
    made = []

    def open_zeroconf(interfaces):
        fake = FakeZeroconf(interfaces, **kwargs)
        made.append(fake)
        return fake

    return open_zeroconf, made


async def test_register_publishes_the_configured_address_and_port():
    open_zeroconf, made = opener()
    await ZeroconfAdvertiser(open_zeroconf).register(advertisement())
    info = made[0].registered[0]
    assert info.type == SERVICE_TYPE
    assert info.name == f"school-1.{SERVICE_TYPE}"
    assert info.server == "school-1.local."
    assert info.port == 8443
    assert info.parsed_addresses() == ["127.0.0.1"]


async def test_register_binds_the_responder_to_the_site_interface():
    open_zeroconf, made = opener()
    await ZeroconfAdvertiser(open_zeroconf).register(advertisement())
    assert made[0].interfaces == ("127.0.0.1",)


async def test_txt_carries_only_the_version_and_the_site_id():
    open_zeroconf, made = opener()
    await ZeroconfAdvertiser(open_zeroconf).register(advertisement())
    assert made[0].registered[0].decoded_properties == {"version": "1", "site_id": "school-1"}


async def test_record_carries_no_pin_token_model_or_credential():
    open_zeroconf, made = opener()
    await ZeroconfAdvertiser(open_zeroconf).register(advertisement())
    info = made[0].registered[0]
    published = b"".join((info.text, info.name.encode(), (info.server or "").encode())).lower()
    for secret in (b"sha256/", b"token", b"key", b"bearer", b"password", b"qwen"):
        assert secret not in published


async def test_duplicate_name_is_renamed_and_keeps_the_true_site_id():
    open_zeroconf, made = opener(taken={f"school-1.{SERVICE_TYPE}"})
    await ZeroconfAdvertiser(open_zeroconf).register(advertisement())
    info = made[0].registered[0]
    assert made[0].allow_name_change is True
    assert info.name == f"school-1-2.{SERVICE_TYPE}"
    assert info.decoded_properties["site_id"] == "school-1"


async def test_registering_twice_is_refused():
    open_zeroconf, _made = opener()
    advertiser = ZeroconfAdvertiser(open_zeroconf)
    await advertiser.register(advertisement())
    with pytest.raises(AdvertiseError, match="already registered"):
        await advertiser.register(advertisement())


async def test_failed_registration_closes_the_responder():
    open_zeroconf, made = opener(fail=OSError("no multicast on this interface"))
    with pytest.raises(AdvertiseError, match="registration failed"):
        await ZeroconfAdvertiser(open_zeroconf).register(advertisement())
    assert made[0].closed == 1


async def test_unregister_withdraws_the_record_and_closes():
    open_zeroconf, made = opener()
    advertiser = ZeroconfAdvertiser(open_zeroconf)
    await advertiser.register(advertisement())
    await advertiser.unregister()
    assert made[0].unregistered == made[0].registered
    assert made[0].closed == 1


async def test_unregister_without_register_is_a_no_op():
    open_zeroconf, made = opener()
    await ZeroconfAdvertiser(open_zeroconf).unregister()
    assert made == []


async def test_unregister_is_idempotent():
    open_zeroconf, made = opener()
    advertiser = ZeroconfAdvertiser(open_zeroconf)
    await advertiser.register(advertisement())
    await advertiser.unregister()
    await advertiser.unregister()
    assert made[0].closed == 1


async def test_advertiser_can_be_reused_after_unregister():
    open_zeroconf, made = opener()
    advertiser = ZeroconfAdvertiser(open_zeroconf)
    await advertiser.register(advertisement())
    await advertiser.unregister()
    await advertiser.register(advertisement())
    assert len(made) == 2
