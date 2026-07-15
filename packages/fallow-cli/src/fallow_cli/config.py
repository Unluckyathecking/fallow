"""Configuration resolution for the ``flw`` CLI.

Resolution order (highest priority first):

- **coordinator URL**: ``--coordinator-url`` flag → ``FLW_COORDINATOR_URL`` env →
  ``coordinator_url`` in the config file (default ``~/.fallow/cli.toml``).
- **admin key**: ``FLW_ADMIN_KEY`` env → ``admin_key`` in the config file. There
  is deliberately **no** admin-key flag: a flag would leak the secret into shell
  history and process listings.

``FLW_CONFIG_FILE`` overrides the config-file path (handy for tests and for
running against several coordinators). Everything is injected (env mapping, file
path) so resolution is pure and deterministic under test.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from fallow_cli.errors import EXIT_AUTH, CliError
from fallow_protocol import FallowModel

DEFAULT_CONFIG_PATH = Path.home() / ".fallow" / "cli.toml"
ENV_COORDINATOR_URL = "FLW_COORDINATOR_URL"
ENV_ADMIN_KEY = "FLW_ADMIN_KEY"
ENV_CONFIG_FILE = "FLW_CONFIG_FILE"

_KNOWN_KEYS = frozenset({"coordinator_url", "admin_key"})


class CliConfig(FallowModel):
    """Fully resolved CLI configuration."""

    coordinator_url: str
    admin_key: str | None = None


def _read_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CliError(f"could not read config file {path}: {exc}") from exc
    unknown = set(data) - _KNOWN_KEYS
    if unknown:
        keys = ", ".join(sorted(unknown))
        raise CliError(f"config file {path}: unknown key(s): {keys}")
    return data


def _pick_str(data: Mapping[str, object], key: str, path: Path) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CliError(f"config file {path}: '{key}' must be a string")
    return value


def _validate_url(url: str) -> str:
    if not (url.startswith("http://") or url.startswith("https://")):
        raise CliError(f"invalid coordinator URL '{url}': must start with http:// or https://")
    return url.rstrip("/")


def load_config(
    cli_coordinator_url: str | None,
    env: Mapping[str, str],
    *,
    config_path: Path | None = None,
) -> CliConfig:
    """Resolve configuration from flag, environment, then config file."""
    path = config_path or Path(env.get(ENV_CONFIG_FILE) or DEFAULT_CONFIG_PATH)
    file_data = _read_file(path)
    coordinator_url = (
        cli_coordinator_url
        or env.get(ENV_COORDINATOR_URL)
        or _pick_str(file_data, "coordinator_url", path)
    )
    if not coordinator_url:
        raise CliError(
            "no coordinator URL configured; pass --coordinator-url, set "
            f"{ENV_COORDINATOR_URL}, or add coordinator_url to {path}"
        )
    admin_key = env.get(ENV_ADMIN_KEY) or _pick_str(file_data, "admin_key", path)
    return CliConfig(coordinator_url=_validate_url(coordinator_url), admin_key=admin_key)


def require_admin_key(config: CliConfig) -> str:
    """Return the admin key or raise a friendly error explaining how to set it."""
    if not config.admin_key:
        raise CliError(
            f"no admin key configured; set the {ENV_ADMIN_KEY} environment variable "
            "or admin_key in your config file. Never pass it as a flag — it would "
            "leak into shell history.",
            exit_code=EXIT_AUTH,
        )
    return config.admin_key
