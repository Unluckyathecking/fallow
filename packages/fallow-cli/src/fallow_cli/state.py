"""Shared plumbing for the ``flw`` command modules.

``CliState`` carries the resolved global options plus the HTTP transports.
The transports are test seams: ``fallow_cli.main`` reads its
``_ADMIN_TRANSPORT`` / ``_DOWNLOAD_TRANSPORT`` globals when a command starts
and hands them over here, so command modules never import ``main``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated

import httpx
import typer

from fallow_cli.client import AdminClient
from fallow_cli.config import CliConfig, load_config, require_admin_key
from fallow_cli.errors import CliError

HTTP_TIMEOUT = httpx.Timeout(30.0)

ModelIdOpt = Annotated[str, typer.Option("--model-id")]


@dataclass(frozen=True)
class CliState:
    """Resolved global options, attached to the typer context."""

    coordinator_url: str | None
    json_output: bool
    admin_transport: httpx.BaseTransport | None = None
    download_transport: httpx.BaseTransport | None = None


def cli_state(ctx: typer.Context) -> CliState:
    obj = ctx.obj
    if not isinstance(obj, CliState):  # pragma: no cover - defensive
        raise CliError("internal error: CLI state not initialised")
    return obj


def resolve(state: CliState) -> CliConfig:
    return load_config(state.coordinator_url, dict(os.environ))


def make_admin_client(state: CliState, config: CliConfig, *, direct: bool = False) -> AdminClient:
    key = require_admin_key(config)
    client = httpx.Client(
        base_url=config.coordinator_url,
        timeout=HTTP_TIMEOUT,
        transport=state.admin_transport,
        trust_env=not direct,
    )
    return AdminClient(client, key)


def make_download_client(state: CliState) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=None),
        transport=state.download_transport,
    )


@contextmanager
def guard(state: CliState, *, direct: bool = False) -> Iterator[AdminClient]:
    """Build the admin client and translate CliError into a clean exit."""
    try:
        with make_admin_client(state, resolve(state), direct=direct) as client:
            yield client
    except CliError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(exc.exit_code) from exc


@contextmanager
def guard_direct(state: CliState) -> Iterator[tuple[httpx.Client, str]]:
    """Yield a raw direct admin transport and the resolved key.

    Same sources and same no-proxy transport as ``site join-bundles``; a raw
    client rather than an :class:`AdminClient` because ADR 094 does not own
    ``client.py``, so the site status route has no typed method there.
    """
    try:
        config = resolve(state)
        key = require_admin_key(config)
        with httpx.Client(
            base_url=config.coordinator_url,
            timeout=HTTP_TIMEOUT,
            transport=state.admin_transport,
            trust_env=False,
        ) as client:
            yield client, key
    except CliError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(exc.exit_code) from exc


@contextmanager
def guard_local(state: CliState) -> Iterator[None]:
    """Guard commands that do local work before any admin client is built."""
    try:
        yield
    except CliError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(exc.exit_code) from exc
