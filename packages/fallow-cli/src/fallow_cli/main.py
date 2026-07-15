"""flw entrypoint (module L1, built in Wave 2)."""

import typer

app = typer.Typer(name="flw", help="Fallow — opportunistic private AI compute layer.")


@app.callback()
def _root() -> None:
    """Fallow CLI. Subcommands land in Wave 2 (enroll, models, jobs, status, bench)."""
