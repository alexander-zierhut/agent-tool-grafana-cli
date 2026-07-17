"""`graf metrics` — PromQL against Prometheus/Mimir, through the datasource proxy.

Same tunnel as Loki (:mod:`.logs`), different backend and — this is the whole
reason this module exists separately rather than sharing a helper with Loki's
query path — **different units**. Verified live:

* Loki's proxy timestamps are **nanoseconds** (:meth:`TimeRange.loki`).
* Prometheus/Mimir's proxy timestamps are **seconds** (:meth:`TimeRange.prometheus`).

Mixing the two up is *silent*: Loki answers a seconds-scale timestamp with
``{"status":"success","data":{"result":[]}}`` — no error, just an empty result
that looks exactly like "nothing happened here". Every call in this file goes
through ``window.prometheus()`` for that reason; never pass a raw int between
layers, and never reach for ``window.loki()`` here even though the two look
interchangeable at a glance.

Verified live paths (`spike/VERIFIED_FINDINGS.md`, Grafana 13.0.3), all through
``client.ds_proxy(uid, ...)``:

    /api/v1/query?query=&time=              instant
    /api/v1/query_range?query=&start=&end=&step=   range
    /api/v1/labels, /api/v1/label/{name}/values     (__name__ has 841 values live)
    /api/v1/metadata                          metric name -> [{type, help, unit}]
    /api/v1/series?match[]=                   real series carrying a selector

841 metric names live is the design pressure behind `list`/`labels`: a bare dump
is a context-window accident, so both default to ``--limit`` and take
``--filter``/``--describe`` to turn a wall of names into something choosable.
"""

from __future__ import annotations

import math
import time as _time
from datetime import datetime, timezone
from typing import Any

import typer
from agentcli import OutputFormat

from .. import loki
from ._shared import ctx_obj, need_datasource, need_window

app = typer.Typer(no_args_is_help=True, help="PromQL against Prometheus/Mimir.")

#: Prometheus hard-errors a query_range above this many points. Verified live —
#: this is not a guess, it is the server's own ceiling.
_MAX_POINTS = 11000

#: The *target* the default step aims for: dense enough to see a spike, far
#: enough under `_MAX_POINTS` that no reasonable --since blows past the ceiling.
_TARGET_POINTS = 1000

#: `up`'s "unhealthy" signal, opt-in only (`--exit-code`). Deliberately far from
#: the 0-9 error-code band, same convention as drone's `build wait --exit-code`
#: (20-29): "a target is down" is an OBSERVATION this command made successfully,
#: not a failure of the command itself.
EXIT_TARGETS_DOWN = 20

_RESERVED_ROW_KEYS = {"value", "values", "timestamp", "time", "points"}


# ---------------------------------------------------------------------------
# pure-ish helpers — kept local to this module (it owns no shared domain file)
# ---------------------------------------------------------------------------


def _pair(raw: Any) -> tuple[float | None, Any]:
    """Prometheus's ``[unix_seconds, "value_as_string"]`` -> ``(ts, value)``.

    The value stays a string (Prometheus sends it as one, including for
    ``NaN``/``+Inf`` — coercing to float here would raise on exactly the
    samples most worth seeing).
    """
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None, None
    ts_raw, val = raw[0], raw[1]
    try:
        ts = float(ts_raw)
    except (TypeError, ValueError):
        ts = None
    return ts, val


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _flatten(data: dict | None) -> list[dict]:
    """Turn ``{resultType, result}`` into one row per series (vector/matrix) or
    one row total (scalar/string) — metric labels alongside their value(s).

    Prometheus's native shape makes the caller branch on `resultType` before
    they can read a single number; this does that branching once so every
    command in this file (and the JSON this CLI hands back) does not have to.
    A `matrix` row keeps its `values` as a list rather than exploding into one
    row per point (unlike Loki's `parse_streams`) — a range query can return
    thousands of points per series, and one row per point would make `results`
    the size of the raw response for no readability gain; the series is the
    natural unit here, the log line is Loki's.
    """
    if not isinstance(data, dict):
        return []
    result_type = data.get("resultType")
    result = data.get("result")

    if result_type == "vector" and isinstance(result, list):
        rows = []
        for item in result:
            if not isinstance(item, dict):
                continue
            ts, val = _pair(item.get("value"))
            row = dict(item.get("metric") or {})
            row["value"] = val
            row["timestamp"] = ts
            row["time"] = _iso(ts)
            rows.append(row)
        return rows

    if result_type == "matrix" and isinstance(result, list):
        rows = []
        for item in result:
            if not isinstance(item, dict):
                continue
            row = dict(item.get("metric") or {})
            points = [_pair(p) for p in (item.get("values") or [])]
            row["values"] = [{"timestamp": ts, "time": _iso(ts), "value": val} for ts, val in points]
            row["points"] = len(row["values"])
            rows.append(row)
        return rows

    if result_type in ("scalar", "string"):
        ts, val = _pair(result)
        return [{"value": val, "timestamp": ts, "time": _iso(ts)}]

    return []


def _default_step(seconds: float) -> float:
    """Pick a `query_range` step, in seconds, from the window length.

    Prometheus errors outright above `_MAX_POINTS`. Hardcoding any single step
    (say, 15s) works for an hour and fails for a week, so this scales with the
    window instead: `_TARGET_POINTS` is comfortably under the ceiling for any
    window, which is the whole point of computing rather than hardcoding it.
    """
    if seconds <= 0:
        return 1.0
    return max(1.0, math.ceil(seconds / _TARGET_POINTS))


def _fmt_step(step: float) -> str:
    return str(int(step)) if step == int(step) else str(step)


def _row_metric(row: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in row.items() if k not in _RESERVED_ROW_KEYS)


def _row_value(row: dict) -> Any:
    if "value" in row:
        return row["value"]
    if "values" in row:
        return f"{row.get('points', 0)} points"
    return None


_RESULT_COLUMNS = [("metric", _row_metric), ("value", _row_value), ("time", "time")]


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


@app.command("query")
def run_query(
    ctx: typer.Context,
    query: str = typer.Option(..., "--query", "-q", help="PromQL expression."),
    range_: bool = typer.Option(
        False, "--range", help="query_range (a time series) instead of an instant query."
    ),
    since: str = typer.Option(None, "--since", help="How far back the window starts, e.g. 1h, 30m."),
    start: str = typer.Option(None, "--from", help="Explicit window start (RFC3339, unix ts, or 'now')."),
    end: str = typer.Option(None, "--to", help="Explicit window end / instant time. Default: now."),
    step: str = typer.Option(
        None, "--step",
        help="query_range resolution step: seconds, or a duration like '30s'/'5m'. "
             "Default: computed from the window so the point count stays under Prometheus's cap.",
    ),
    datasource: str = typer.Option(None, "--datasource", "-d", help="Metrics datasource uid or name."),
) -> None:
    """Run a PromQL query. Instant by default; `--range` for a time series.

    The window (`--since`/`--from`/`--to`) always resolves, even for an instant
    query: its END becomes the instant `time=` sent to Prometheus, so `--to
    2026-07-10T00:00:00Z` answers "what was this metric at that instant"
    without needing a separate flag for it.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ds = need_datasource(obj, datasource, kind="metrics")
    window = need_window(obj, since=since, start=start, end=end)
    start_s, end_s = window.prometheus()

    if range_:
        step_s = step or _fmt_step(_default_step(window.seconds()))
        sent = {"query": query, "start": start_s, "end": end_s, "step": step_s}
        payload = client.ds_proxy(ds["uid"], "api/v1/query_range", params=sent)
    else:
        sent = {"query": query, "time": end_s}
        payload = client.ds_proxy(ds["uid"], "api/v1/query", params=sent)

    data = payload.get("data") if isinstance(payload, dict) else None
    result_type = data.get("resultType") if isinstance(data, dict) else None
    rows = _flatten(data)

    if obj.output == OutputFormat.table:
        obj.emitter.message(
            f"{ds['name']} ({ds['uid']}) · {result_type or 'no result'} · "
            f"window {window.describe()['start']} .. {window.describe()['end']}"
        )
        obj.emitter.emit(rows, columns=_RESULT_COLUMNS, empty="(no results)")
        return

    out: dict[str, Any] = {
        "datasource": {"uid": ds["uid"], "name": ds["name"]},
        "window": window.describe(),
        "sent": sent,
        "resultType": result_type,
        "count": len(rows),
        "results": rows,
    }
    if isinstance(payload, dict) and payload.get("warnings"):
        out["warnings"] = payload["warnings"]
    obj.emitter.emit(out)


@app.command("list")
def list_metrics(
    ctx: typer.Context,
    filter_: str = typer.Option(None, "--filter", help="Only names containing this substring (case-insensitive)."),
    limit: int = typer.Option(
        None, "--limit", "-n",
        help="Max names returned (default: the configured default_limit; 0 = no cap).",
    ),
    describe: bool = typer.Option(
        False, "--describe",
        help="Enrich each name with its type + help text (one extra call to /api/v1/metadata) — "
             "the difference between a wall of names and something you can actually pick from.",
    ),
    since: str = typer.Option(None, "--since", help="Only names seen since this far back."),
    start: str = typer.Option(None, "--from", help="Explicit window start."),
    end: str = typer.Option(None, "--to", help="Explicit window end."),
    datasource: str = typer.Option(None, "--datasource", "-d", help="Metrics datasource uid or name."),
) -> None:
    """Metric names this datasource knows (`__name__`).

    841 live on this instance — unbounded output here is a context-window
    accident, hence `--limit`/`--filter`. `--since`/`--from`/`--to` are OPTIONAL
    (unlike Loki's label endpoints, which the CLI always time-bounds): omitted,
    Prometheus answers from its full retained data, which is what "841 live"
    above was counted against.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ds = need_datasource(obj, datasource, kind="metrics")
    limit_n = obj.config.default_limit if limit is None else limit

    params: dict = {}
    window = None
    if since or start or end:
        window = need_window(obj, since=since, start=start, end=end)
        s, e = window.prometheus()
        params = {"start": s, "end": e}

    payload = client.ds_proxy(ds["uid"], "api/v1/label/__name__/values", params=params)
    names = sorted(loki.parse_label_values(payload))
    total = len(names)

    matched = names
    if filter_:
        needle = filter_.lower()
        matched = [n for n in names if needle in n.lower()]

    shown = matched[:limit_n] if limit_n else matched

    metadata: dict[str, list[dict]] = {}
    if describe and shown:
        meta_payload = client.ds_proxy(ds["uid"], "api/v1/metadata")
        meta_data = meta_payload.get("data") if isinstance(meta_payload, dict) else None
        metadata = meta_data if isinstance(meta_data, dict) else {}

    def row_for(name: str) -> dict:
        if not describe:
            return {"metric": name}
        entries = metadata.get(name) or []
        first = entries[0] if entries else {}
        return {"metric": name, "type": first.get("type"), "help": first.get("help")}

    rows = [row_for(n) for n in shown]
    columns = [("metric", "metric"), ("type", "type"), ("help", "help")] if describe else [("metric", "metric")]

    if obj.output == OutputFormat.table:
        summary = f"{ds['name']} ({ds['uid']}) · {total} metric names"
        if filter_:
            summary += f", {len(matched)} matching {filter_!r}"
        if limit_n and len(matched) > limit_n:
            summary += f" · showing {len(shown)} (--limit {limit_n})"
        obj.emitter.message(summary)
        obj.emitter.emit(rows, columns=columns, empty="(no metrics)")
        return

    out: dict[str, Any] = {
        "datasource": {"uid": ds["uid"], "name": ds["name"]},
        "total": total,
        "filter": filter_,
        "matched": len(matched),
        "returned": len(shown),
        "metrics": rows,
    }
    if window:
        out["window"] = window.describe()
    if limit_n and len(matched) > limit_n:
        out["note"] = (
            f"{len(matched)} names matched; showing {len(shown)} (--limit {limit_n}). "
            f"Raise --limit or narrow --filter to see the rest."
        )
    obj.emitter.emit(out)


@app.command("describe")
def describe_metric(
    ctx: typer.Context,
    metric: str = typer.Argument(..., help="Metric name, e.g. up, node_cpu_seconds_total."),
    sample: int = typer.Option(5, "--sample", help="How many example series to include, from /api/v1/series."),
    since: str = typer.Option(None, "--since", help="Window to look for series in (default: default_since)."),
    start: str = typer.Option(None, "--from", help="Explicit window start."),
    end: str = typer.Option(None, "--to", help="Explicit window end."),
    datasource: str = typer.Option(None, "--datasource", "-d", help="Metrics datasource uid or name."),
) -> None:
    """What IS this metric, and how do you slice it?

    Three calls answer that: `/api/v1/metadata` (type + help text — best-effort,
    scraped from a target's `/metrics` endpoint, so a metric can be real and
    still have no metadata if the target that describes it is down), the label
    NAMES actually present on it (from a `/api/v1/series` sample, since
    Prometheus has no "labels of this metric" endpoint), and a handful of real
    series so you can see values, not just names.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ds = need_datasource(obj, datasource, kind="metrics")
    window = need_window(obj, since=since, start=start, end=end)
    start_s, end_s = window.prometheus()

    meta_payload = client.ds_proxy(ds["uid"], "api/v1/metadata", params={"metric": metric})
    meta_data = meta_payload.get("data") if isinstance(meta_payload, dict) else None
    entries = (meta_data or {}).get(metric) if isinstance(meta_data, dict) else None

    series_payload = client.ds_proxy(
        ds["uid"], "api/v1/series", params={"match[]": metric, "start": start_s, "end": end_s}
    )
    series_data = series_payload.get("data") if isinstance(series_payload, dict) else None
    series = series_data if isinstance(series_data, list) else []

    label_names: set[str] = set()
    for item in series:
        if isinstance(item, dict):
            label_names.update(item.keys())
    label_names.discard("__name__")

    sample_n = max(0, sample)
    out: dict[str, Any] = {
        "metric": metric,
        "datasource": {"uid": ds["uid"], "name": ds["name"]},
        "window": window.describe(),
        "metadata": entries or [],
        "labelNames": sorted(label_names),
        "seriesFound": len(series),
        "sample": series[:sample_n],
    }
    if not entries:
        out["note"] = (
            f"no /api/v1/metadata entry for {metric!r} — metadata is best-effort and "
            f"target-scoped, so this can be a real, queryable metric anyway. "
            f"{len(series)} series were found in the window above regardless of that."
        )
    obj.emitter.emit(out)


@app.command("labels")
def list_labels(
    ctx: typer.Context,
    label: str = typer.Option(None, "--label", help="Show this label's VALUES instead of the label names."),
    since: str = typer.Option(None, "--since", help="Only labels/values seen since this far back."),
    start: str = typer.Option(None, "--from", help="Explicit window start."),
    end: str = typer.Option(None, "--to", help="Explicit window end."),
    limit: int = typer.Option(
        None, "--limit", "-n", help="Max values returned with --label (default: default_limit)."
    ),
    datasource: str = typer.Option(None, "--datasource", "-d", help="Metrics datasource uid or name."),
) -> None:
    """Label NAMES this datasource carries, or (with `--label`) that label's values.

    Not the same question as `metrics list`: `list` is metric NAMES
    (`__name__`'s own values); this is everything else you can slice a PromQL
    query by — `job`, `instance`, and whatever else targets export.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ds = need_datasource(obj, datasource, kind="metrics")

    params: dict = {}
    window = None
    if since or start or end:
        window = need_window(obj, since=since, start=start, end=end)
        s, e = window.prometheus()
        params = {"start": s, "end": e}

    if label:
        payload = client.ds_proxy(ds["uid"], f"api/v1/label/{label}/values", params=params)
        values = sorted(loki.parse_label_values(payload))
        total = len(values)
        limit_n = obj.config.default_limit if limit is None else limit
        shown = values[:limit_n] if limit_n else values
        rows = [{"value": v} for v in shown]

        if obj.output == OutputFormat.table:
            summary = f"{ds['name']} ({ds['uid']}) · label {label!r} · {total} values"
            if limit_n and total > limit_n:
                summary += f" · showing {len(shown)} (--limit {limit_n})"
            obj.emitter.message(summary)
            obj.emitter.emit(rows, columns=[("value", "value")], empty="(no values)")
            return

        out = {
            "datasource": {"uid": ds["uid"], "name": ds["name"]},
            "label": label,
            "total": total,
            "returned": len(shown),
            "values": shown,
        }
        if window:
            out["window"] = window.describe()
        if limit_n and total > limit_n:
            out["note"] = f"{total} values; showing {len(shown)} (--limit {limit_n})."
        obj.emitter.emit(out)
        return

    payload = client.ds_proxy(ds["uid"], "api/v1/labels", params=params)
    names = sorted(loki.parse_label_values(payload))
    rows = [{"label": n} for n in names]

    if obj.output == OutputFormat.table:
        obj.emitter.message(f"{ds['name']} ({ds['uid']}) · {len(names)} label names")
        obj.emitter.emit(rows, columns=[("label", "label")], empty="(no labels)")
        return

    out = {"datasource": {"uid": ds["uid"], "name": ds["name"]}, "count": len(names), "labels": names}
    if window:
        out["window"] = window.describe()
    obj.emitter.emit(out)


@app.command("up")
def up_check(
    ctx: typer.Context,
    datasource: str = typer.Option(None, "--datasource", "-d", help="Metrics datasource uid or name."),
    exit_code: bool = typer.Option(
        False, "--exit-code",
        help=f"Exit {EXIT_TARGETS_DOWN} if any target is down. Default: exit 0 regardless — a down "
             f"target is an OBSERVATION this command made successfully, not a CLI failure.",
    ),
) -> None:
    """Run `up` and report which scrape targets are down.

    This is the single most useful PromQL query there is, and the metrics half
    of "does this project work?" (`graf logs sources` / `graf scan` cover the
    logs half). Every target Prometheus/Mimir scrapes reports `up` == 1 or 0 —
    no PromQL knowledge required to ask "is anything broken right now".
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ds = need_datasource(obj, datasource, kind="metrics")

    payload = client.ds_proxy(ds["uid"], "api/v1/query", params={"query": "up", "time": _time.time()})
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = _flatten(data)

    targets = []
    for row in rows:
        labels = {k: v for k, v in row.items() if k not in _RESERVED_ROW_KEYS and k not in ("instance", "job")}
        targets.append(
            {
                "up": row.get("value") == "1",
                "instance": row.get("instance"),
                "job": row.get("job"),
                "labels": labels,
            }
        )

    down = [t for t in targets if not t["up"]]
    up_n = len(targets) - len(down)

    if obj.output == OutputFormat.table:
        obj.emitter.message(f"{ds['name']}: {up_n} up, {len(down)} down (of {len(targets)} checked)")
        obj.emitter.emit(
            down,
            columns=[
                ("job", "job"), ("instance", "instance"),
                ("labels", lambda r: ", ".join(f"{k}={v}" for k, v in (r.get("labels") or {}).items())),
            ],
            empty="(all targets up)" if targets else "(no targets found — nothing is being scraped, or `up` itself is unreachable)",
        )
    else:
        out: dict[str, Any] = {
            "datasource": {"uid": ds["uid"], "name": ds["name"]},
            "checked": len(targets),
            "up": up_n,
            "down": len(down),
            "downTargets": down,
            "targets": targets,
        }
        if not targets:
            out["note"] = (
                "the `up` query returned no series — either nothing is being scraped by this "
                "Prometheus/Mimir, or something is wrong with the query path itself. This is NOT "
                "the same as 'everything is fine'; treat an empty result as unknown, not healthy."
            )
        obj.emitter.emit(out)

    if exit_code and down:
        raise typer.Exit(code=EXIT_TARGETS_DOWN)
