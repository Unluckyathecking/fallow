"""Coordinator FastAPI app factory (module I1, Wave 3).

Public API:

- :func:`create_app` — assemble the coordinator ``FastAPI`` app from a
  :class:`CoordinatorConfig` (agent + admin + gateway + modelserve routes).
- :func:`build_app` — no-arg factory for ``uvicorn ... --factory``.
- :class:`CoordinatorConfig` / :func:`load_config` — frozen config + TOML/env loader.
"""

from fallow_coordinator.app.config import CoordinatorConfig, load_config
from fallow_coordinator.app.factory import build_app, create_app

__all__ = [
    "CoordinatorConfig",
    "build_app",
    "create_app",
    "load_config",
]
