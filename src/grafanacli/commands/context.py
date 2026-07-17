"""Session context — sticky option defaults applied to later commands.

A context is a small map of option-name -> value (e.g. ``datasource=P1A2B…``).
Once set, those values become the *default* for matching options on later
commands, so you stop repeating ``--datasource`` on every `logs`/`metrics` call.
Explicit flags always win, and ``--no-context`` ignores the context for one
command (both wired in ``cli.py``).

**This diverges from the sibling CLIs, and the divergence is load-bearing.**
Drone's and OpenProject's context is one flat dict — a repo slug or a project id
is stable no matter which server profile is active. Grafana's is not: datasource
UIDs are minted **per org**, so the same Loki is a different UID in every org.
`Config.context` is therefore a read-only property scoped to the *active
profile* (see `config.py`'s module docstring), and this module never assigns to
it directly — it goes through `config.set_context()` / `config.clear_context()`,
which key the write by `active_profile_name()` for you. Switch `--profile` and
the sticky `--datasource` switches with it, rather than pointing at a UID that
does not exist in the new org and returning a 404 that blames the datasource.

The mechanism itself is Click's ``default_map``, wired up in
``cli.py::_context_default_map``: a key applies to a command **only** if that
command has an *option* whose name matches the key exactly. There is no error
when it doesn't — the key simply does nothing, forever. That is why
``KNOWN_KEYS`` below is not documentation: a tree-wide test asserts every entry
has a real consumer somewhere in the command tree.
"""

from __future__ import annotations

import os

import typer

from ..config import config_path
from ..errors import OpError
from ..timerange import parse_duration
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)

#: Keys the context can hold. Each MUST match the *name* of an option on at
#: least one real command (test-enforced tree-wide) — a key with no consumer is
#: a silent no-op, not a warning. These three are deliberately not guesses:
#: `datasource` scopes `logs`/`metrics` commands to a UID without retyping it;
#: `since` scopes the lookback window the same way `settings set-since` scopes
#: the global default, but per-profile and overridable per command; `folder`
#: scopes dashboard/alert commands that create or list within one folder.
KNOWN_KEYS = ["datasource", "since", "folder"]

_KEY_HELP = {
    "datasource": "Default datasource uid (or name) for logs/metrics commands. Per-org — see below.",
    "since": "Default lookback window for this profile, e.g. 1h, 15m, 2d. Validated like `settings set-since`.",
    "folder": "Default folder uid for dashboard/alert commands.",
}

#: The only rung a context value can come from today — read from the config
#: file and nowhere else. Emitted per key anyway, because `show`'s job is
#: answering "why is this scoped wrong?", and an answer that omits *where the
#: value came from* is half of one.
SOURCE_SAVED = "saved"


def _validate(key: str, value: str) -> str:
    """Validate at `context set` time, not at Click's converter.

    A context value is injected as an option's *default*, so a bad one would
    otherwise surface later, on some unrelated command, pointing at a flag the
    caller never passed. Reject it here, where the caller can still see what
    they typed.
    """
    v = (value or "").strip()
    if not v:
        raise OpError(f"context key '{key}' cannot be empty — use `context clear` to remove it.")
    if key == "since":
        parse_duration(v)  # raises ValidationError with the accepted syntax
    return v


@app.command("set")
def set_context(
    ctx: typer.Context,
    datasource: str = typer.Option(None, "--datasource", help=_KEY_HELP["datasource"]),
    since: str = typer.Option(None, "--since", help=_KEY_HELP["since"]),
    folder: str = typer.Option(None, "--folder", help=_KEY_HELP["folder"]),
) -> None:
    """Set/merge sticky defaults for the ACTIVE PROFILE.

        grafana-cli context set --datasource P1A2B3C4

    Then `grafana-cli logs query` behaves like `grafana-cli logs query --datasource P1A2B3C4`,
    but only while this profile stays active — switch `--profile` and this
    context does not follow, because the UID would not mean anything there.
    """
    obj = ctx_obj(ctx)
    cfg = obj.config

    updates = {k: v for k, v in (("datasource", datasource), ("since", since), ("folder", folder)) if v is not None}
    if not updates:
        raise OpError(
            f"nothing to set — pass e.g. --datasource <uid>. Known keys: {', '.join(KNOWN_KEYS)}"
        )

    merged = dict(cfg.context)
    merged.update({k: _validate(k, v) for k, v in updates.items()})
    cfg.set_context(merged)
    cfg.save()
    obj.emitter.emit({"status": "context updated", "profile": cfg.active_profile_name(), "context": merged})


@app.command()
def show(ctx: typer.Context) -> None:
    """Show the active PROFILE's context — each value, and where it came from.

    Run this FIRST whenever output looks wrongly scoped: this is implicit state
    that changes results, and nothing echoes it back on a normal command. Always
    names the profile it belongs to — a context that looks empty may simply
    belong to a *different* profile than the one you think is active.

    Every key is reported as `{"value": ..., "from": ...}` — `from` is `saved`
    for everything, for now. Read `applies` before believing any of it:
    `--no-context` suspends the whole context for one command, and then these
    values are saved but NOT in force.
    """
    obj = ctx_obj(ctx)
    cfg = obj.config
    values = cfg.context
    obj.emitter.emit(
        {
            "profile": cfg.active_profile_name(),
            "context": {k: {"value": v, "from": SOURCE_SAVED} for k, v in values.items()},
            # Whether the context is live right now, not merely non-empty:
            # `--no-context` (popped into this env var by cli.py) suspends it.
            "applies": bool(values) and os.environ.get("GRAFANACLI_NO_CONTEXT") != "1",
            "knownKeys": KNOWN_KEYS,
            "configPath": str(config_path()),
        }
    )


@app.command()
def clear(ctx: typer.Context) -> None:
    """Clear the active profile's context entirely. Other profiles' contexts are untouched."""
    obj = ctx_obj(ctx)
    cfg = obj.config
    profile = cfg.active_profile_name()
    cfg.clear_context()
    cfg.save()
    obj.emitter.emit({"status": "context cleared", "profile": profile})
