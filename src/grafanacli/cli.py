"""Top-level Typer application."""

from __future__ import annotations

import os
import sys

import typer
from agentcli import OutputFormat, print_error
from agentcli.errors import DryRun, OpError

from . import __version__
from .appctx import AppContext


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


app = typer.Typer(
    name="graf",
    help=(
        "Agent-friendly CLI for Grafana: find what you can get logs from, read them, "
        "work out what is wrong, and set up an alert so you hear about it next time.\n\n"
        "Output is JSON on stdout by default (errors are JSON on stderr with a non-zero "
        "exit code); add `-o table` or trim with `--fields label,values`. Start with "
        "`graf logs sources` — you cannot query what you cannot name.\n\n"
        "New here / no context? Run `graf guide` for the full playbook."
    ),
    epilog="Learn more:  `graf guide`  ·  `graf guide <topic>`  ·  `graf <group> --help`",
    no_args_is_help=True,
    add_completion=False,
    # A security decision, not cosmetics: locals hold the API token, and a
    # pretty traceback would print it to the terminal (and into CI logs).
    pretty_exceptions_show_locals=False,
)

# Remembered so the central handler can render errors in the format the user
# asked for, even when the failure predates the command.
_ERROR_FORMAT = OutputFormat.json


@app.callback()
def _root(
    ctx: typer.Context,
    output: OutputFormat = typer.Option(
        None, "--output", "-o",
        help="Output format: json (default), table, markdown, csv. Also --format/-f, anywhere on the line.",
    ),
    fields: str = typer.Option(
        None, "--fields", "--columns",
        help="Comma-separated fields to return, e.g. 'label,values'. Works anywhere on the line.",
    ),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Configuration profile. One profile per Grafana ORG — tokens cannot cross orgs.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Mutating commands: print the request that would be sent and exit."
    ),
    stream: bool = typer.Option(False, "--stream", help="Stream results as NDJSON."),
    no_context: bool = typer.Option(
        False, "--no-context", help="Ignore the saved session context for this command."
    ),
    no_color: bool = typer.Option(False, "--no-color", help="Disable coloured output."),
    version: bool = typer.Option(
        None, "--version", "-V", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    if profile:
        os.environ["GRAFANACLI_PROFILE"] = profile

    # The first-run prompt gate is belt-and-braces, and every clause is
    # load-bearing:
    #  - meta subcommands must never prompt (they are what you run to fix things);
    #  - stdin AND stdout must be TTYs (stdin alone fires the prompt into `| jq`
    #    from a real terminal and hangs it forever);
    #  - CI=true is the universal signal.
    meta = ctx.invoked_subcommand in ("settings", "guide", "install", "context")
    interactive = (
        not meta
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("CI") != "true"
    )
    ctx.obj = AppContext(output=output, color=not no_color, interactive=interactive)
    global _ERROR_FORMAT
    _ERROR_FORMAT = ctx.obj.emitter.fmt

    if os.environ.get("GRAFANACLI_NO_CONTEXT") != "1":
        active = ctx.obj.config.context
        if active:
            # `auth`, `server` and `org` are excluded alongside the meta groups:
            # they are what you run to diagnose things, and a sticky default
            # silently rescoping a diagnostic is the last thing you want when lost.
            dm = _context_default_map(
                ctx.command,
                active,
                skip={"context", "settings", "guide", "install", "auth", "server", "org"},
            )
            ctx.default_map = {**(ctx.default_map or {}), **dm}


def _context_default_map(group, values: dict, skip: set) -> dict:
    """Build a Click default_map from the active context: for every command with
    an option whose name is a context key, use that value as the default."""
    dmap: dict = {}
    for name, cmd in getattr(group, "commands", {}).items():
        if name in skip:
            continue
        if hasattr(cmd, "commands"):
            sub = _context_default_map(cmd, values, skip)
            if sub:
                dmap[name] = sub
        else:
            opt_names = {p.name for p in cmd.params if getattr(p, "param_type_name", "") == "option"}
            matched = {k: v for k, v in values.items() if k in opt_names}
            if matched:
                dmap[name] = matched
    return dmap


# The reserved namespace. Anything here is stripped from argv before Click parses
# it, so NO command may declare an option with these names -- it could never
# receive one. tests/test_globals_unit.py enforces that across the whole tree.
# (OpenProject learned this the hard way: `attach download --output f.pdf`
# silently wrote to the wrong file, exit 0, for four releases. Which is why every
# file destination in THIS tool is spelled `--out`.)
_FORMAT_FLAGS = ("--format", "-f", "--output", "-o")
_FIELDS_FLAGS = ("--fields", "--columns")
_BOOL_FLAGS = ("--dry-run", "--stream", "--no-context")


def _pop_globals(argv: list[str]) -> tuple[str | None, str | None, set[str], list[str]]:
    """Extract global flags from anywhere on the line, so they work after a
    subcommand too. Honours ``--`` to stop parsing."""
    out: list[str] = []
    fmt: str | None = None
    fields: str | None = None
    bools: set[str] = set()
    i, stop = 0, False

    def take_value(idx: int) -> tuple[str | None, int]:
        return (argv[idx + 1], idx + 1) if idx + 1 < len(argv) else (None, idx)

    while i < len(argv):
        a = argv[i]
        if not stop and a == "--":
            stop = True
            out.append(a)
        elif not stop and a in _FORMAT_FLAGS:
            fmt, i = take_value(i)
        elif not stop and a in _FIELDS_FLAGS:
            fields, i = take_value(i)
        elif not stop and a in _BOOL_FLAGS:
            bools.add(a.lstrip("-"))
        elif not stop and any(a.startswith(p + "=") for p in _FORMAT_FLAGS):
            fmt = a.split("=", 1)[1]
        elif not stop and any(a.startswith(p + "=") for p in _FIELDS_FLAGS):
            fields = a.split("=", 1)[1]
        else:
            out.append(a)
        i += 1
    return fmt, fields, bools, out


# ---- command groups (imported here to avoid circular imports) ----
from .commands import (  # noqa: E402
    alert,
    auth,
    context as context_cmd,
    dashboard,
    datasource,
    guide,
    install,
    logs,
    metrics,
    notify,
    org,
    raw,
    scan,
    server,
    settings,
)

app.command("guide", help="Built-in operating guide — how to use this CLI without external docs.")(guide.guide)

# Top-level, because this is the thing you actually came here to do. The whole
# point of the tool is "I deployed, is it broken?" -> `graf scan`; burying that
# under a group would make the answer harder to find than the problem.
app.command("scan", help="Is this healthy? Find errors, panics and deprecations in one pass.")(scan.scan)

app.add_typer(logs.app, name="logs", help="Logs: discover sources, query, and find similar problems.")
app.add_typer(metrics.app, name="metrics", help="Metrics: PromQL against Prometheus/Mimir.")
app.add_typer(dashboard.app, name="dashboard", help="Dashboards: find, read, create.")
app.add_typer(alert.app, name="alert", help="Alert rules — including `alert route`: will it reach you?")
app.add_typer(notify.app, name="notify", help="Contact points, notification policies, silences.")
app.add_typer(datasource.app, name="datasource", help="Datasources: list, inspect, health-check.")
app.add_typer(org.app, name="org", help="Organisations — and why a token only ever sees one.")
app.add_typer(auth.app, name="auth", help="Log in, log out, inspect credentials.")
app.add_typer(server.app, name="server", help="Health, version — and `server doctor`.")
app.add_typer(raw.app, name="raw", help="Escape hatch: call any API endpoint directly.")
app.add_typer(settings.app, name="settings", help="View & change CLI settings.")
app.add_typer(context_cmd.app, name="context", help="Sticky session defaults (datasource, etc.), per profile.")
app.add_typer(install.app, name="install", help="Integrate with other tools (e.g. `install claude`).")


def main() -> None:
    import json as _json

    fmt, fields, bools, argv = _pop_globals(sys.argv[1:])
    if fmt is not None:
        os.environ["GRAFANACLI_CLI_FORMAT"] = fmt
    if fields is not None:
        os.environ["GRAFANACLI_CLI_FIELDS"] = fields
    if "dry-run" in bools:
        os.environ["GRAFANACLI_DRY_RUN"] = "1"
    if "stream" in bools:
        os.environ["GRAFANACLI_STREAM"] = "1"
    if "no-context" in bools:
        os.environ["GRAFANACLI_NO_CONTEXT"] = "1"
    try:
        app(args=argv)
    except DryRun as dr:
        sys.stdout.write(_json.dumps({"dryRun": True, "request": dr.request}, indent=2, default=str) + "\n")
        sys.exit(0)
    except OpError as exc:
        print_error(exc, _ERROR_FORMAT)
        sys.exit(exc.exit_code)
    except ValueError as exc:
        # e.g. OutputFormat.coerce on a bad --format. A usage error, not a crash.
        print_error(OpError(str(exc)), _ERROR_FORMAT)
        sys.exit(1)
    except KeyboardInterrupt:  # pragma: no cover
        print_error(OpError("interrupted"), _ERROR_FORMAT)
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
