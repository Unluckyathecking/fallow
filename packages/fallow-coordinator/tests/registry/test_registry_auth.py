"""API-key issuance/authentication, allowlist round-trip, and model catalogue."""

from registry_helpers import ADMIN_KEY, make_manifest

from fallow_coordinator.registry import SqliteRegistry


async def test_api_key_allowlist_round_trips(registry: SqliteRegistry) -> None:
    key = await registry.create_api_key("embed-fleet", ("m1", "m2"))

    info = await registry.authenticate_api_key(key)
    assert info is not None
    assert info.name == "embed-fleet"
    assert info.model_allowlist == ("m1", "m2")
    assert info.is_admin is False


async def test_api_key_without_allowlist_is_unrestricted(registry: SqliteRegistry) -> None:
    key = await registry.create_api_key("full-access", None)

    info = await registry.authenticate_api_key(key)
    assert info is not None
    assert info.model_allowlist is None


async def test_admin_key_authenticates_without_a_row(registry: SqliteRegistry) -> None:
    info = await registry.authenticate_api_key(ADMIN_KEY)
    assert info is not None
    assert info.is_admin is True
    assert info.model_allowlist is None


async def test_unknown_api_key_is_rejected(registry: SqliteRegistry) -> None:
    assert await registry.authenticate_api_key("bogus") is None


async def test_put_and_get_model(registry: SqliteRegistry) -> None:
    manifest = make_manifest("qwen2.5-7b")
    await registry.put_model(manifest, blob_path="/blobs/qwen.gguf")

    record = await registry.get_model("qwen2.5-7b")
    assert record is not None
    assert record.blob_path == "/blobs/qwen.gguf"
    assert record.enabled is True
    assert record.manifest == manifest

    fetched = await registry.get_manifest("qwen2.5-7b")
    assert fetched == manifest
    assert await registry.get_model("absent") is None
    assert (await registry.list_models()) == (manifest,)


async def test_assignments_drive_desired_models(registry: SqliteRegistry) -> None:
    await registry.set_assignments("agent-x", ("m1", "m2"))
    assert await registry.desired_models("agent-x") == ("m1", "m2")

    await registry.set_assignments("agent-x", ("m3",))
    assert await registry.desired_models("agent-x") == ("m3",)
