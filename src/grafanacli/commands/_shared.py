"""Helpers shared by command modules."""

from __future__ import annotations

import typer

from ..appctx import AppContext
from ..timerange import TimeRange


def ctx_obj(ctx: typer.Context) -> AppContext:
    """The AppContext built by the root callback.

    Falls back to constructing one so a command can be unit-tested (or invoked
    via CliRunner) without the root callback having run.
    """
    obj = getattr(ctx, "obj", None)
    if obj is None:
        obj = AppContext()
        ctx.obj = obj
    return obj


def need_window(
    obj: AppContext,
    *,
    since: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> TimeRange:
    """Resolve the time window from the usual flags plus the saved default."""
    return TimeRange.resolve(
        since=since, start=start, end=end, default_since=obj.config.default_since
    )


def need_datasource(obj: AppContext, ref: str | None, *, kind: str = "logs") -> dict:
    """Resolve `--datasource`: explicit > sticky context > the only candidate.

    The sticky rung is read here rather than injected as a Click default because
    the context is keyed by profile, and `--profile` is parsed in the same pass —
    so the default map is built before we know which profile's context applies.
    """
    from .. import sources

    return sources.resolve(obj.client(), ref or sources.context_datasource(obj, kind), kind=kind)


def parse_label_args(pairs: list[str] | None) -> dict[str, str]:
    """Turn repeated ``--label k=v`` into a matcher dict.

    Values keep any operator prefix (`~` regex, `!` negate) — `loki.build_selector`
    reads it. So `--label hostname=~web.*` survives intact.
    """
    from agentcli.errors import ValidationError

    out: dict[str, str] = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise ValidationError(
                f"--label wants key=value, got {raw!r}. "
                f"Examples: --label systemd_unit=docker.service, --label hostname=~web.*"
            )
        key, value = raw.split("=", 1)
        out[key.strip()] = value.strip()
    return out
