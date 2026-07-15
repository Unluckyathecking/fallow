"""``python -m fallow_coordinator serve --config <path>`` entrypoint (module I1).

Loads a :class:`CoordinatorConfig` from a TOML file (with ``FALLOW_COORD_*`` env
overrides), builds the app, and runs uvicorn on the configured bind address. The
app object is passed to uvicorn directly (not the ``--factory`` string form), so
the lifespan opens the stores and starts the background loops normally.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from fallow_coordinator.app import create_app, load_config


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fallow_coordinator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="run the coordinator HTTP server")
    serve.add_argument("--config", required=True, help="path to coordinator.toml")
    args = parser.parse_args(argv)

    if args.command == "serve":
        config = load_config(args.config)
        app = create_app(config)
        uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
