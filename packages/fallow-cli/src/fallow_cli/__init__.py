"""fallow-cli: the ``flw`` command-line interface.

Public API:

- :data:`app` — the typer application (entry point ``fallow_cli.main:app``).
- :class:`AdminClient` — typed client for the coordinator admin API.
- :func:`load_config` / :class:`CliConfig` — configuration resolution.
- :class:`CliError` — the user-facing error type.

The admin-API contract this CLI is built against is specified in
``docs/admin-api.md``; wave-3 implements the coordinator side from it.
"""

from fallow_cli.client import AdminClient
from fallow_cli.config import CliConfig, load_config
from fallow_cli.errors import CliError
from fallow_cli.main import app

__all__ = ["AdminClient", "CliConfig", "CliError", "app", "load_config"]
