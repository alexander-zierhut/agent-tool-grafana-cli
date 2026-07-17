"""Top-level Typer application. Scaffold — the real surface lands next."""

from __future__ import annotations

import typer

from . import __version__


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


app = typer.Typer(
    name="graf",
    help="Agent-friendly CLI for Grafana — logs first. (Scaffold.)",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,  # locals hold the API token
)


@app.callback()
def _root(
    version: bool = typer.Option(
        None, "--version", "-V", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    pass


@app.command()
def guide() -> None:
    """Built-in operating guide (coming next)."""
    typer.echo("graf — scaffold. The operating guide lands with the log commands.")


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
