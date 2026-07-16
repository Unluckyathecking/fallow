"""``python -m fallow_coordinator {serve,promote} --config <path>`` entrypoint.

``serve`` loads a :class:`CoordinatorConfig` from a TOML file (with
``FALLOW_COORD_*`` env overrides), builds the app, and runs uvicorn on the
configured bind address. The app object is passed to uvicorn directly (not the
``--factory`` string form), so the lifespan opens the stores and starts the
background loops normally.

``promote`` is the manual warm-standby failover step (ADR 057): it installs an
exported snapshot as this coordinator's live ``db_path`` so a fresh ``serve``
resumes from the last exported state. Run it with the local coordinator stopped.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from fallow_coordinator.app import create_app, load_config
from fallow_coordinator.app.promote import PromoteError, promote


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fallow_coordinator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the coordinator HTTP server")
    serve.add_argument("--config", required=True, help="path to coordinator.toml")

    promote_cmd = subparsers.add_parser(
        "promote", help="install a warm-standby snapshot as this coordinator's live state DB"
    )
    promote_cmd.add_argument("--config", required=True, help="path to coordinator.toml")
    promote_cmd.add_argument(
        "--snapshot", help="snapshot to install (default: standby_path from the config)"
    )
    promote_cmd.add_argument(
        "--force",
        action="store_true",
        help="overwrite a db_path newer than the snapshot (guards a still-running primary)",
    )

    args = parser.parse_args(argv)

    if args.command == "serve":
        config = load_config(args.config)
        app = create_app(config)
        uvicorn.run(app, host=config.host, port=config.port)
    elif args.command == "promote":
        _promote(args.config, args.snapshot, args.force)


def _promote(config_path: str, snapshot: str | None, force: bool) -> None:
    """Resolve the snapshot source from the config (or ``--snapshot``) and install it."""
    config = load_config(config_path)
    source = Path(snapshot) if snapshot else config.standby_path
    if source is None:
        raise SystemExit("promote: no --snapshot given and standby_path is unset in the config")
    try:
        promote(source, config.db_path, force=force)
    except PromoteError as exc:
        raise SystemExit(f"promote failed: {exc}") from exc
    print(f"promoted {source} -> {config.db_path}; start the coordinator to resume")


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
