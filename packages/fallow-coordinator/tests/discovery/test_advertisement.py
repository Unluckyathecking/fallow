"""The record the coordinator would publish, built from the configured bind.

No socket is opened here: ``build_advertisement`` only resolves the interface and
shapes the record.
"""

from __future__ import annotations

import socket

import pytest

from fallow_coordinator.discovery import (
    MAX_LABEL_LENGTH,
    SERVICE_TYPE,
    AdvertiseError,
    build_advertisement,
)


def test_loopback_bind_becomes_one_a_record():
    ad = build_advertisement(site_id="school-1", host="127.0.0.1", port=8443)
    assert ad.addresses == ("127.0.0.1",)
    assert ad.port == 8443
    assert ad.interface == "127.0.0.1"
    assert ad.instance_name == f"school-1.{SERVICE_TYPE}"
    assert ad.server == "school-1.local."


def test_txt_carries_only_the_version_and_the_site_id():
    ad = build_advertisement(site_id="school-1", host="127.0.0.1", port=8443)
    assert ad.txt == {"version": "1", "site_id": "school-1"}


def test_txt_is_a_fresh_mapping_per_read():
    ad = build_advertisement(site_id="school-1", host="127.0.0.1", port=8443)
    ad.txt["site_id"] = "tampered"
    assert ad.txt["site_id"] == "school-1"


def test_dual_stack_host_yields_one_address_per_family(monkeypatch):
    def resolve(*_args, **_kwargs):
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::5", 0, 0, 4)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    ad = build_advertisement(site_id="school-1", host="coordinator.school", port=8443)
    assert ad.addresses == ("10.0.0.5", "fd00::5")


def test_link_local_scope_is_stripped(monkeypatch):
    def resolve(*_args, **_kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%en0", 0, 0, 4))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    ad = build_advertisement(site_id="school-1", host="coordinator.school", port=8443)
    assert ad.addresses == ("fe80::1",)


def test_repeated_resolution_entries_are_one_address(monkeypatch):
    def resolve(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 17, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    ad = build_advertisement(site_id="school-1", host="coordinator.school", port=8443)
    assert ad.addresses == ("10.0.0.5",)


def test_ambiguous_interface_is_rejected(monkeypatch):
    def resolve(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.6", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(AdvertiseError, match="ambiguous"):
        build_advertisement(site_id="school-1", host="coordinator.school", port=8443)


def test_wildcard_interface_is_rejected(monkeypatch):
    def resolve(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("0.0.0.0", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(AdvertiseError, match="wildcard"):
        build_advertisement(site_id="school-1", host="0.0.0.0", port=8443)


def test_unresolvable_interface_is_rejected(monkeypatch):
    def resolve(*_args, **_kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(AdvertiseError, match="does not resolve"):
        build_advertisement(site_id="school-1", host="nope.invalid", port=8443)


def test_empty_resolution_is_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(AdvertiseError, match="does not resolve"):
        build_advertisement(site_id="school-1", host="empty.invalid", port=8443)


@pytest.mark.parametrize(
    ("site_id", "label"),
    [
        ("school-1", "school-1"),
        ("Site 7 / North", "Site-7---North"),
        ("--edges--", "edges"),
        ("st.andrews", "st-andrews"),
    ],
)
def test_site_id_folds_into_a_dns_label(site_id, label):
    ad = build_advertisement(site_id=site_id, host="127.0.0.1", port=8443)
    assert ad.label == label
    # The authoritative id is unchanged; only the display label is folded.
    assert ad.txt["site_id"] == site_id


def test_long_site_id_is_truncated_to_a_valid_label():
    ad = build_advertisement(site_id="a" * 128, host="127.0.0.1", port=8443)
    assert ad.label == "a" * MAX_LABEL_LENGTH


def test_truncation_never_leaves_a_trailing_hyphen():
    ad = build_advertisement(
        site_id="a" * (MAX_LABEL_LENGTH - 1) + "- tail", host="127.0.0.1", port=1
    )
    assert ad.label == "a" * (MAX_LABEL_LENGTH - 1)


def test_site_id_with_no_usable_character_is_rejected():
    with pytest.raises(AdvertiseError, match="usable"):
        build_advertisement(site_id="///", host="127.0.0.1", port=8443)
