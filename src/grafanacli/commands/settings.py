"""`graf settings` — every setting has a sane default or is asked once.

Three settings live on `Config` (see `config.py`): `default_format` (asked once,
interactively, on first run — see `appctx._ask_default_format`), `default_since`
("1h" — how far back a query looks when `--since` is omitted) and `default_limit`
(100 — the cap on returned rows/lines). None of them can ever be silently unset:
`Config.load()` re-applies the module constants for anything missing from disk.
"""

from __future__ import annotations

import typer
from agentcli import OutputFormat

from ..config import Config, config_path
from ..errors import OpError
from ..spec import SPEC, credentials
from ..timerange import parse_duration
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)


@app.command()
def show(ctx: typer.Context) -> None:
    """Show every setting, its value, and where it came from."""
    obj = ctx_obj(ctx)
    cfg: Config = obj.config
    obj.emitter.emit(
        {
            "defaultFormat": cfg.default_format or "json (default — not yet chosen)",
            "defaultSince": cfg.default_since,
            "defaultLimit": cfg.default_limit,
            "activeProfile": cfg.active_profile_name(),
            "credentialBackend": credentials.backend_name(),
            "configPath": str(config_path()),
        }
    )


@app.command("set-format")
def set_format(
    ctx: typer.Context,
    fmt: str = typer.Argument(..., help="json | table | markdown | csv"),
) -> None:
    """Set the default output format."""
    obj = ctx_obj(ctx)
    try:
        chosen = OutputFormat.coerce(fmt)
    except ValueError as exc:
        raise OpError(str(exc)) from exc
    obj.config.default_format = chosen.value
    obj.config.save()
    obj.emitter.emit({"status": "saved", "defaultFormat": chosen.value, "configPath": str(config_path())})


@app.command("set-since")
def set_since(
    ctx: typer.Context,
    since: str = typer.Argument(..., help="Default lookback window when --since is omitted, e.g. 1h, 15m, 2d."),
) -> None:
    """Set the default lookback window for logs/metrics queries.

    Runs through the same parser every query uses (`timerange.parse_duration`),
    so a bad value is rejected HERE, at write time, with the caller still looking
    at what they typed — not on some later `logs query` that inherits it silently
    and fails somewhere less obvious.
    """
    obj = ctx_obj(ctx)
    parse_duration(since)  # raises ValidationError — the actual validation
    obj.config.default_since = since
    obj.config.save()
    obj.emitter.emit({"status": "saved", "defaultSince": since, "configPath": str(config_path())})


@app.command("set-limit")
def set_limit(
    ctx: typer.Context,
    limit: int = typer.Argument(..., help="Default cap on returned rows/lines when --limit is omitted."),
) -> None:
    """Set the default result limit.

    The live instance carries ~87 units across ~21 hosts (per-label cardinality,
    see `spike/VERIFIED_FINDINGS.md`), so an unbounded default is a context-window
    accident waiting to happen — see `config.DEFAULT_LIMIT`.
    """
    obj = ctx_obj(ctx)
    if limit <= 0:
        raise OpError(f"limit must be a positive integer, got {limit}.")
    obj.config.default_limit = limit
    obj.config.save()
    obj.emitter.emit({"status": "saved", "defaultLimit": limit, "configPath": str(config_path())})


@app.command()
def path(ctx: typer.Context) -> None:
    """Print the config file path."""
    ctx_obj(ctx).emitter.emit({"configPath": str(config_path()), "configDir": str(SPEC.config_dir())})
