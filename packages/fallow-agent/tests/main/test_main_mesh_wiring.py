"""Assembly picks the model store from the flag: blob by default, mesh when on."""

from __future__ import annotations

from pathlib import Path

import httpx
from main_helpers import make_settings

from fallow_agent.main.assembly import AgentAssembly
from fallow_agent.main.identity import IdentityState
from fallow_agent.main.seams import RuntimeSeams
from fallow_agent.mesh import MeshModelStore
from fallow_agent.modelcache import HttpModelStore

_IDENTITY = IdentityState(agent_id="agent-1", device_token="tok-1")


def _assembly(settings_overrides: dict[str, object]) -> AgentAssembly:
    tmp = Path(settings_overrides.pop("_tmp"))  # type: ignore[arg-type]
    settings = make_settings(tmp, **settings_overrides)
    return AgentAssembly(settings, RuntimeSeams(), on_fatal=lambda: None)


async def test_mesh_off_by_default_uses_the_blob_store(tmp_path: Path) -> None:
    assembly = _assembly({"_tmp": tmp_path})
    async with httpx.AsyncClient() as http:
        store = assembly._build_modelstore(http, _IDENTITY)
    assert type(store) is HttpModelStore


async def test_mesh_enabled_wraps_the_blob_store(tmp_path: Path) -> None:
    assembly = _assembly({"_tmp": tmp_path, "mesh": {"enabled": True, "signing_key": "shared-key"}})
    async with httpx.AsyncClient() as http:
        store = assembly._build_modelstore(http, _IDENTITY)
    assert isinstance(store, MeshModelStore)
