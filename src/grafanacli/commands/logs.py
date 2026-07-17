"""`grafana-cli logs` — discover what you can query, then query it.

This is where the family's killer-feature principle (*derive the answer the API
refuses to give*) lands directly. Grafana will answer a LogQL query the moment
you already know the right selector, and will never tell you what selectors
exist — see :mod:`..sources`. So this group has a deliberate shape:

* ``sources`` answers "what can I even get logs from?" — the command an agent
  with zero context should run first, ever.
* ``query`` and ``tail`` are the read path once you know (or have guessed) a
  selector.
* ``search`` and ``similar`` exist because "I don't know what my thing is
  called" and "has this happened before" are themselves search problems, not
  a LogQL syntax problem — turning a guess into a selector is the product.
* ``levels`` is the cheap first look: which of my sources has the errors,
  before spending a query on any one of them.

**Two different defaults for "which datasource".** ``query`` and ``tail`` need
exactly one, resolved the normal way (``--datasource`` > sticky context > the
only candidate) via :func:`_shared.need_datasource` — a LogQL query only ever
targets one backend. ``sources``, ``search``, ``similar`` and ``levels`` are
the opposite: their whole job is "which of my datasources", so defaulting to
one (sticky context included) would answer a narrower question than the one
being asked. They fan out over every queryable log datasource unless
``--datasource`` explicitly restricts them, and — like `sources.survey` —
capture a dead backend's error in the payload rather than letting it blank out
the healthy ones.

**Output.** Every command here emits the full JSON contract except `query
--raw`, which prints bare log lines as text — logs are prose, and that is the
one place this tool breaks its own rule on purpose. Every payload that used a
time window embeds it (`window.describe()`): Loki's label endpoints are
time-bounded, so a result that does not say which window it asked about is
half an answer.
"""

from __future__ import annotations

import re
import shlex
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import typer

from .. import analysis, loki
from .. import sources as src
from ..errors import ConfigError, NotFoundError, OpError, ValidationError
from ..timerange import TimeRange
from ._shared import ctx_obj, need_datasource, need_window, parse_label_args

app = typer.Typer(no_args_is_help=True)

#: `logs sources` table columns — the summary a human scanning `-o table` needs:
#: not the full label/value breakdown (that only makes sense as JSON), just
#: enough to know which datasource to look at next.
_SOURCES_COLUMNS = ["name", "uid", "type", "labelCount", "reachable"]

#: `detected_level` is filterable but not a real stream label (see loki.py's
#: module docstring) — listing the known values here is a help-text nicety
#: only, never a gate, because Loki adds to the set between versions.
_LEVEL_HINT = ", ".join(loki.KNOWN_LEVELS)


# ---------------------------------------------------------------------------
# shared helpers — kept here, not in _shared.py, because they encode
# LogQL-building decisions specific to this command group
# ---------------------------------------------------------------------------


def _require_loki(ds: dict) -> None:
    """Refuse before sending a Loki-shaped request to a datasource that isn't one.

    ``sources.resolve`` matches an explicit ``--datasource`` uid/name against
    *every* log-capable type, including ones this CLI only ``recognises`` (e.g.
    Elasticsearch) but cannot query — because hiding a real datasource would be
    a worse answer than naming it and saying so (see ``sources.classify``). That
    is correct for `logs sources`, whose job is to report what exists. It is
    wrong for `query`/`tail`, which are about to speak Loki's dialect down the
    proxy — sending that to Elasticsearch would come back as a confusing
    datasource-shaped error instead of a clear one from here.
    """
    if ds.get("logs") != "supported":
        raise ConfigError(
            f"{ds.get('name')} ({ds.get('type')}) is a recognised log datasource, but this "
            f"CLI only speaks Loki today. Pick a Loki datasource — `grafana-cli logs sources` shows "
            f"which ones qualify."
        )


def _pool(client, datasource: str | None) -> list[dict]:
    """The datasources a fan-out command should visit.

    An explicit ``--datasource`` restricts to one, resolved and validated the
    same way every other command does. With none, every QUERYABLE log
    datasource in the org — these commands exist specifically to answer
    "which one", so silently narrowing to a single guess (even a sticky
    context) would defeat the command.
    """
    if datasource:
        ds = src.resolve(client, datasource, kind="logs")
        _require_loki(ds)
        return [ds]
    pool = [d for d in src.log_datasources(client) if d.get("logs") == "supported"]
    if not pool:
        raise ConfigError(
            "no queryable log datasource in this org. `grafana-cli logs sources` shows what exists."
        )
    return pool


def _match_all_query(client, ds: dict, window: TimeRange, **build_kwargs) -> str:
    """Build a LogQL query with a match-all selector for one datasource.

    Shared by every command that wants "everything on this datasource, filtered
    by a pipeline stage" rather than a specific label matcher (`similar`,
    `levels`, `search --content`, and `query`/`tail` when the caller passed no
    ``--label`` at all). Picking the match-all label costs one ``/labels`` call
    plus one ``/label/*/values`` call per label — real network traffic, cheap on
    the live instance's 4 labels but not free, so it is centralised here instead
    of repeated at every call site.
    """
    names = src.loki_labels(client, ds["uid"], window)
    values = {name: src.loki_label_values(client, ds["uid"], name, window) for name in names}
    match_all_label = loki.pick_match_all_label(values)
    return loki.build_query({}, match_all_label=match_all_label, **build_kwargs)


def _resolve_logql(
    client,
    ds: dict,
    window: TimeRange,
    *,
    label: list[str] | None,
    contains: list[str] | None,
    exclude: list[str] | None,
    regex: str | None,
    level: str | None,
    query_: str | None,
) -> str:
    """The LogQL to actually send: the raw ``--query``, or the builder flags.

    The two are mutually exclusive on purpose — silently letting ``--query``
    win while ignoring ``--label``/``--contains``/etc that the caller also typed
    would look like they were honoured and were not. With no matchers at all,
    this falls through to :func:`_match_all_query` rather than raising, so
    ``grafana-cli logs query -d loki`` (no selector) works the way `logs sources` and
    `logs levels` already do — see `loki.pick_match_all_label`.
    """
    builder_used = bool(label) or bool(contains) or bool(exclude) or bool(regex) or bool(level)
    if query_:
        if builder_used:
            raise ValidationError(
                "--query is raw LogQL and mutually exclusive with --label/--contains/--exclude/"
                "--regex/--level — it is used VERBATIM, so mixing it with builder flags would "
                "silently drop whichever ones lost. Pass one or the other."
            )
        return query_

    matchers = parse_label_args(label)
    if matchers:
        return loki.build_query(matchers, contains=contains, excludes=exclude, regex=regex, level=level)
    return _match_all_query(client, ds, window, contains=contains, excludes=exclude, regex=regex, level=level)


def _now_ns() -> int:
    """The current instant in Loki's nanosecond units.

    Goes through `TimeRange`'s own conversion rather than hand-rolling
    ``int(time.time() * 1e9)`` — a float64 loses precision at the nanosecond
    scale (see timerange.py's module docstring), and `tail` calls this every
    poll, so a sloppy version here would compound across a long-running watch.
    """
    now = datetime.now(timezone.utc)
    return TimeRange(now - timedelta(seconds=1), now).loki()[1]


def _ds_ref(ds: dict) -> dict:
    return {"uid": ds.get("uid"), "name": ds.get("name"), "type": ds.get("type")}


# ---------------------------------------------------------------------------
# sources — THE killer feature
# ---------------------------------------------------------------------------


@app.command()
def sources(
    ctx: typer.Context,
    since: str = typer.Option(None, "--since", help="How far back to look, e.g. 1h, 2d, 30m. Default: your configured default-since."),
    from_: str = typer.Option(None, "--from", help="Explicit window start (RFC 3339, unix timestamp, or 'now'). Overrides --since."),
    to: str = typer.Option(None, "--to", help="Explicit window end. Default: now."),
    sample: int = typer.Option(5, "--sample", help="Example values shown per label."),
    datasource: str = typer.Option(None, "--datasource", "-d", help="Restrict to one datasource (uid or name). Default: every log datasource in the org."),
) -> None:
    """"What can I even get logs from?" — run this before anything else.

    Grafana will not answer this question anywhere, UI or API: you are expected
    to already know which datasource, and which label, to query. This
    enumerates every log-capable datasource in the org and, for each Loki one,
    every label it has IN THIS WINDOW along with how many distinct values it
    carries — cardinality, not just names. On the reference instance two of
    Loki's four labels have exactly one value: "you can filter on `job`" is
    worthless advice when `job` never varies, and cardinality is the only way
    to tell the useless labels from the ones worth building a selector on.

    The window matters more than it looks: `/labels` and `/label/*/values` are
    time-bounded, so this command's answer can differ at 09:00 and at 17:00.
    That is why the resolved window is always in the payload, and why
    `--since`/`--from`/`--to` exist here at all rather than this being a
    parameterless "list everything" command.

    A datasource whose backend is down is reported WITH its error, beside the
    ones that are fine — one dead proxy must not blank out a working report.
    A datasource of a recognised-but-unimplemented type (Elasticsearch,
    CloudWatch, ...) is listed too, honestly labelled, rather than hidden:
    pretending it does not exist would be the same non-answer Grafana already
    gives.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    window = need_window(obj, since=since, start=from_, end=to)

    if datasource:
        # Reuse survey()'s per-item error handling rather than reimplementing
        # it for one item: resolve first (so an unknown --datasource fails with
        # sources.resolve's own message), then filter survey's report down to
        # it. Surveying the whole org even for one uid is a few extra calls at
        # most on any real org — trivial next to duplicating error handling
        # that must stay in sync with survey()'s.
        ds = src.resolve(client, datasource, kind="logs")
        full = src.survey(client, window, sample=sample)
        entries = [d for d in full["datasources"] if d.get("uid") == ds.get("uid")]
        payload = {"window": full["window"], "datasources": entries}
    else:
        payload = src.survey(client, window, sample=sample)

    obj.emitter.emit(payload, columns=_SOURCES_COLUMNS)


# ---------------------------------------------------------------------------
# query — the read path
# ---------------------------------------------------------------------------


@app.command()
def query(
    ctx: typer.Context,
    datasource: str = typer.Option(None, "--datasource", "-d", help="Datasource uid or name. Default: sticky context, or the only log datasource."),
    label: list[str] = typer.Option(None, "--label", "-l", help="k=v stream matcher, repeatable. Value may carry an operator prefix: ~ regex, ! negate, e.g. hostname=~web.*"),
    contains: list[str] = typer.Option(None, "--contains", help="Line must contain this substring (LogQL |=), repeatable — ANDed together."),
    exclude: list[str] = typer.Option(None, "--exclude", help="Line must NOT contain this substring (LogQL !=), repeatable."),
    regex: str = typer.Option(None, "--regex", help="Line must match this regex (LogQL |~, RE2 syntax)."),
    level: str = typer.Option(None, "--level", help=f"Filter by detected_level ({_LEVEL_HINT}, not enforced — Loki may add more). Applied as a pipeline stage, never a selector — see the gotcha below."),
    query_: str = typer.Option(None, "--query", "-q", help="Raw LogQL, used VERBATIM. Mutually exclusive with --label/--contains/--exclude/--regex/--level."),
    limit: int = typer.Option(None, "--limit", help="Max lines returned. Default: your configured default-limit."),
    since: str = typer.Option(None, "--since", help="How far back, e.g. 1h, 2d, 30m. Default: your configured default-since."),
    from_: str = typer.Option(None, "--from", help="Explicit window start (RFC 3339, unix timestamp, or 'now'). Overrides --since."),
    to: str = typer.Option(None, "--to", help="Explicit window end. Default: now."),
    direction: str = typer.Option("backward", "--direction", help="backward (newest-first, default) or forward (oldest-first) — which end of the window --limit keeps when there are more matches than that."),
    raw: bool = typer.Option(False, "--raw", help="Print bare log lines as text instead of JSON — the one carve-out from this tool's output contract, because logs are prose."),
) -> None:
    """Read logs from one datasource.

    The LogQL actually sent is ALWAYS echoed back in the payload's ``query``
    field, raw or not — a query an agent cannot see is a query it cannot fix,
    and that includes queries it built itself from ``--label``/``--contains``,
    since the escaping and operator handling happen here, invisibly, unless
    you look.

    GOTCHA worth knowing before reaching for ``--query``: ``detected_level`` is
    not a real stream label — it does not appear in ``/labels`` — yet Loki
    derives it at query time and lets you filter on it. ``{detected_level=
    "error"}`` in a raw selector silently matches NOTHING; ``--level error``
    (or, in raw LogQL, ``| detected_level="error"`` as a pipeline stage after
    the selector) is the only form that works. `loki.build_query` gets this
    right for you when you use ``--level``; a hand-written ``--query`` will not
    warn you if you get it wrong.

    With no ``--label`` at all (and no ``--query``), this picks the
    highest-cardinality label discovered in the window and queries
    ``{that_label=~".+"}`` — Loki rejects an empty ``{}`` selector outright, so
    "give me everything" has to be spelled as *some* real matcher. Run
    `grafana-cli logs sources` first if that surprises you.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ds = need_datasource(obj, datasource, kind="logs")
    _require_loki(ds)
    window = need_window(obj, since=since, start=from_, end=to)

    if direction not in ("backward", "forward"):
        raise ValidationError(f"--direction must be 'backward' or 'forward', got {direction!r}.")
    lim = obj.config.default_limit if limit is None else limit

    logql = _resolve_logql(
        client, ds, window,
        label=label, contains=contains, exclude=exclude, regex=regex, level=level, query_=query_,
    )

    start_ns, end_ns = window.loki()
    resp = client.ds_proxy(
        ds["uid"], "loki/api/v1/query_range",
        params={"query": logql, "start": start_ns, "end": end_ns, "limit": lim, "direction": direction},
    )
    records = loki.parse_streams(resp)

    if raw:
        for rec in records:
            typer.echo(rec["line"])
        return

    payload = {
        "window": window.describe(),
        "query": logql,
        "datasource": _ds_ref(ds),
        "count": len(records),
        "lines": records,
    }
    if lim and len(records) >= lim:
        payload["note"] = (
            f"hit --limit ({lim}); the window may hold more than this. Narrow "
            f"--since/--from/--to, raise --limit, or tighten the selector."
        )
    obj.emitter.emit(payload)


# ---------------------------------------------------------------------------
# tail — polling, not a live stream
# ---------------------------------------------------------------------------


@app.command()
def tail(
    ctx: typer.Context,
    datasource: str = typer.Option(None, "--datasource", "-d", help="Datasource uid or name. Default: sticky context, or the only log datasource."),
    label: list[str] = typer.Option(None, "--label", "-l", help="k=v stream matcher, repeatable. Same syntax as `logs query`."),
    contains: list[str] = typer.Option(None, "--contains", help="Line must contain this substring, repeatable."),
    exclude: list[str] = typer.Option(None, "--exclude", help="Line must NOT contain this substring, repeatable."),
    regex: str = typer.Option(None, "--regex", help="Line must match this regex."),
    level: str = typer.Option(None, "--level", help="Filter by detected_level, e.g. error."),
    query_: str = typer.Option(None, "--query", "-q", help="Raw LogQL, used verbatim. Mutually exclusive with the builder flags above."),
    interval: float = typer.Option(2.0, "--interval", help="Seconds between polls."),
    limit: int = typer.Option(None, "--limit", help="Max lines fetched PER POLL. Default: your configured default-limit."),
) -> None:
    """Follow new log lines — by POLLING, not a live stream.

    Loki's own tail endpoint is a WebSocket, and this CLI's whole HTTP stack is
    a synchronous `httpx.Client`; adding a websocket dependency for one command
    is not a trade worth making. So this calls the exact same `query_range` the
    `query` command uses, on a timer, tracking the newest timestamp it has
    already shown you and asking only for what came after.

    Be honest with yourself about what that means:

    * a line can be up to ``--interval`` seconds late — it only appears at the
      NEXT poll, never sooner;
    * if MORE than ``--limit`` new lines land within one interval, only the
      newest ``--limit`` of them are kept (this polls with ``direction=
      backward``, same as `query`'s default) — the rest, being older than
      the ones already reported and now outside the next poll's start bound,
      are gone for good, not delayed. A bursty source needs a higher
      ``--limit`` or a shorter ``--interval``, not patience.

    Ctrl-C stops it. This deliberately does not catch `KeyboardInterrupt` —
    letting it propagate is what gives you the standard exit code 130 instead
    of this command inventing its own "stopped" status.

    With `--stream` (the global flag), each new line is written as its own
    NDJSON row via `obj.emitter.stream_json`, as it is found. Without it, each
    poll's new lines are emitted together as one JSON batch — so in `-o table`
    or plain JSON mode, expect one printed block per poll that found something,
    not one line at a time.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ds = need_datasource(obj, datasource, kind="logs")
    _require_loki(ds)

    if interval <= 0:
        raise ValidationError(f"--interval must be positive, got {interval!r}.")
    lim = obj.config.default_limit if limit is None else limit

    # The label discovery that `_resolve_logql` may need (for a match-all
    # selector) has nothing to do with the polling window — it just asks "what
    # labels does this datasource have", so a generous, ordinary window is
    # right for it even though the poll loop below uses a much narrower one.
    label_window = need_window(obj, since=obj.config.default_since)
    logql = _resolve_logql(
        client, ds, label_window,
        label=label, contains=contains, exclude=exclude, regex=regex, level=level, query_=query_,
    )

    # First poll looks back `interval` seconds (minimum 1s, so a very small
    # --interval cannot produce an empty/invalid window) to catch anything that
    # landed between process start and the first request. Every poll after
    # that starts one nanosecond past the last line already shown.
    now = datetime.now(timezone.utc)
    start_ns, end_ns = TimeRange(now - timedelta(seconds=max(interval, 1.0)), now).loki()
    last_ns: int | None = None

    while True:
        resp = client.ds_proxy(
            ds["uid"], "loki/api/v1/query_range",
            params={"query": logql, "start": start_ns, "end": end_ns, "limit": lim, "direction": "backward"},
        )
        records = loki.parse_streams(resp)  # newest-first
        fresh = [r for r in records if last_ns is None or r["timestamp"] > last_ns]

        if fresh:
            last_ns = max(r["timestamp"] for r in fresh)
            batch = list(reversed(fresh))  # oldest-first: the order they happened
            if obj.emitter.stream:
                obj.emitter.stream_json(batch)
            else:
                obj.emitter.emit({"query": logql, "new": len(batch), "lines": batch})

        time.sleep(interval)
        start_ns = (last_ns + 1) if last_ns is not None else end_ns
        end_ns = _now_ns()


# ---------------------------------------------------------------------------
# search — turn a guess into a selector
# ---------------------------------------------------------------------------


@app.command()
def search(
    ctx: typer.Context,
    term: str = typer.Argument(..., help="Free text to look for, e.g. 'api' or 'postgres'."),
    datasource: str = typer.Option(None, "--datasource", "-d", help="Restrict to one datasource. Default: every log datasource — this command's job is telling you WHICH one."),
    content: bool = typer.Option(False, "--content", help="Also search log CONTENT (a |~ regex query against a match-all selector), not just label values. Costs one real query per datasource — slower than the default."),
    since: str = typer.Option(None, "--since", help="How far back, e.g. 1h, 2d, 30m. Default: your configured default-since."),
    from_: str = typer.Option(None, "--from", help="Explicit window start. Overrides --since."),
    to: str = typer.Option(None, "--to", help="Explicit window end. Default: now."),
    limit: int = typer.Option(None, "--limit", help="Cap on --content query results per datasource. Default: your configured default-limit."),
) -> None:
    """"I don't know what my thing is called" — find out.

    You know roughly what you are looking for ("the api service", "that
    postgres box") but not the exact label value LogQL needs. This checks
    every label VALUE on every log datasource for the term as a substring —
    ``search api`` finds ``systemd_unit=api.service`` even though you never
    typed the ``.service`` suffix — and, with ``--content``, also runs a real
    query to find which label SETS carry the term in the log lines
    themselves.

    The point is not the list of hits, it is the ``suggestion`` field on each
    one: a ready-to-run ``grafana-cli logs query ...`` command with the right
    ``--datasource``/``--label`` (and ``--contains``, for a content hit) already
    filled in. A label name is not a query; this turns the guess into one.

    Ignores any sticky ``--datasource`` context by default, on purpose — the
    whole reason to run `search` instead of `query` is that you have not
    committed to a datasource yet.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    window = need_window(obj, since=since, start=from_, end=to)
    lim = obj.config.default_limit if limit is None else limit

    needle = (term or "").strip().lower()
    if not needle:
        raise ValidationError("search needs a non-empty term.")

    pool = _pool(client, datasource)
    hits: list[dict] = []
    errors: list[dict] = []

    for ds in pool:
        try:
            names = src.loki_labels(client, ds["uid"], window)
        except OpError as exc:
            # Sibling trap: NotFoundError/AuthError/DatasourceUnreachable/ApiError
            # are all direct subclasses of OpError, not of each other — a
            # narrower except here would look exhaustive and would not be.
            errors.append({"datasource": ds["name"], "uid": ds["uid"], "error": str(exc)})
            continue

        values_by_label: dict[str, list[str]] = {}
        for label_name in names:
            try:
                values = src.loki_label_values(client, ds["uid"], label_name, window)
            except OpError:
                # One label failing to resolve must not blank out the rest —
                # same fan-out contract as `sources`.
                continue
            values_by_label[label_name] = values
            for value in values:
                v = value.lower()
                if needle not in v:
                    continue
                hits.append(
                    {
                        "kind": "label",
                        "datasource": ds["name"],
                        "uid": ds["uid"],
                        "label": label_name,
                        "value": value,
                        "score": 100 if v == needle else (80 if v.startswith(needle) else 50),
                        "suggestion": _suggestion(ds["uid"], {label_name: value}),
                    }
                )

        if content:
            try:
                # Reuses values_by_label from the loop above instead of calling
                # _match_all_query (which would re-fetch labels+values over the
                # wire) -- we already paid for exactly the data it needs.
                match_all_label = loki.pick_match_all_label(values_by_label)
                logql = loki.build_query({}, regex=re.escape(term), match_all_label=match_all_label)
                start_ns, end_ns = window.loki()
                resp = client.ds_proxy(
                    ds["uid"], "loki/api/v1/query_range",
                    params={"query": logql, "start": start_ns, "end": end_ns, "limit": lim, "direction": "backward"},
                )
                records = loki.parse_streams(resp)
            except OpError as exc:
                errors.append({"datasource": ds["name"], "uid": ds["uid"], "kind": "content", "error": str(exc)})
                continue
            for group in _group_by_labels(records):
                hits.append(
                    {
                        "kind": "content",
                        "datasource": ds["name"],
                        "uid": ds["uid"],
                        "labels": group["labels"],
                        "count": group["count"],
                        "example": group["example"],
                        "score": min(99, 40 + group["count"]),
                        "suggestion": _suggestion(ds["uid"], group["labels"], contains=term),
                    }
                )

    hits.sort(key=lambda h: (-h["score"], h.get("datasource") or "", h.get("label") or h.get("kind") or ""))
    obj.emitter.emit(
        {
            "window": window.describe(),
            "term": term,
            "matchCount": len(hits),
            "matches": hits,
            "errors": errors,
        }
    )


def _group_by_labels(records: list[dict]) -> list[dict]:
    """Distinct label sets among matching records, most common first.

    A content hit reports label SETS, not raw lines: "these labels carried the
    term N times" is what turns a hit into a selector via `_suggestion`. A bare
    list of matching lines would leave the reader to reverse-engineer the
    selector by hand — the opposite of what `search` exists to save them from.
    """
    buckets: dict[tuple, dict] = {}
    for rec in records:
        labels = rec.get("labels") or {}
        key = tuple(sorted(labels.items()))
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = {"labels": dict(labels), "count": 0, "example": rec.get("line")}
        bucket["count"] += 1
    return sorted(buckets.values(), key=lambda b: -b["count"])


def _suggestion(uid: str, matchers: dict[str, str], *, contains: str | None = None) -> str:
    """A copy-pasteable `grafana-cli logs query` for one hit.

    This is the actual point of `search`: a label name or a "yes it's in
    there somewhere" is not a query. Shell-quoting every value (`shlex.quote`)
    is not paranoia — systemd unit names, k8s annotations and the search term
    itself can all legitimately contain spaces or quotes.

    ``contains`` (LogQL ``|=``, a literal substring), not ``--regex``: the
    content search this feeds ran with ``regex=re.escape(term)`` specifically
    so that a term containing regex metacharacters (``a.b``, ``a+b``) is
    matched LITERALLY. A suggestion built from the raw, un-escaped term as
    ``--regex`` would silently ask for something broader than what actually
    matched — the suggestion must reproduce the query that found the hit, not
    a similar-looking one.
    """
    parts = ["grafana-cli", "logs", "query", "-d", shlex.quote(uid)]
    for k, v in matchers.items():
        parts.append("-l")
        parts.append(shlex.quote(f"{k}={v}"))
    if contains:
        parts.append("--contains")
        parts.append(shlex.quote(contains))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# similar — has this happened elsewhere?
# ---------------------------------------------------------------------------


@app.command()
def similar(
    ctx: typer.Context,
    line: str = typer.Argument(None, help="The log line to fingerprint (quote it). Omit and use --line or --from-last."),
    line_opt: str = typer.Option(None, "--line", help="Same as the positional LINE — for scripting where a positional string is awkward."),
    from_last: bool = typer.Option(False, "--from-last", help="Use the most recent error-level line instead of typing one."),
    datasource: str = typer.Option(None, "--datasource", "-d", help="Restrict to one datasource. Default: every log datasource — 'elsewhere' means the whole org, not just where you found it."),
    since: str = typer.Option(None, "--since", help="How far back, e.g. 1h, 2d, 30m. Default: your configured default-since."),
    from_: str = typer.Option(None, "--from", help="Explicit window start. Overrides --since."),
    to: str = typer.Option(None, "--to", help="Explicit window end. Default: now."),
    limit: int = typer.Option(None, "--limit", help="Max lines fetched per datasource. Default: your configured default-limit."),
) -> None:
    """"Has this happened elsewhere?" — search by SHAPE, not exact text.

    A raw substring search would only find the exact ids in the line you
    already have. This reduces the line to its shape with
    `analysis.fingerprint` — ids, timestamps, addresses and durations become
    placeholders, so ``connection to 10.0.0.7:5432 failed after 1.2s`` and
    ``connection to 10.0.0.9:5432 failed after 0.4s`` are recognised as the
    SAME problem — then turns that shape into a permissive LogQL regex with
    `analysis.to_regex` and runs it, unanchored, against a match-all selector
    on every log datasource (or one, with ``--datasource``).

    Results are grouped by `analysis.cluster`, which reports which distinct
    label sets (host, unit, container — whichever is most specific) the
    pattern showed up on and how many times: "this happened on one host" and
    "this happened on all twenty-one" look identical in a flat log tail and
    are completely different problems.

    The fingerprint and the regex derived from it are always in the payload —
    the regex especially, since a normalised fingerprint alone does not tell
    you whether `to_regex` generalised more or less than you would have by
    hand.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    window = need_window(obj, since=since, start=from_, end=to)
    lim = obj.config.default_limit if limit is None else limit
    pool = _pool(client, datasource)

    text = (line or line_opt or "").strip()
    if from_last:
        if text:
            raise ValidationError("--from-last picks the seed line itself — do not also pass LINE or --line.")
        text = _latest_error_line(client, pool, window)
    if not text:
        raise ValidationError("give a line to fingerprint: a positional LINE, --line, or --from-last.")

    fp = analysis.fingerprint(text)
    if not fp:
        raise ValidationError(
            "that line fingerprints to nothing (blank after normalisation) — there is no shape "
            "left to build a regex from."
        )
    regex = analysis.to_regex(fp)

    all_records: list[dict] = []
    by_datasource: list[dict] = []
    for ds in pool:
        try:
            logql = _match_all_query(client, ds, window, regex=regex)
            start_ns, end_ns = window.loki()
            resp = client.ds_proxy(
                ds["uid"], "loki/api/v1/query_range",
                params={"query": logql, "start": start_ns, "end": end_ns, "limit": lim, "direction": "backward"},
            )
            records = loki.parse_streams(resp)
        except OpError as exc:
            by_datasource.append({"datasource": ds["name"], "uid": ds["uid"], "error": str(exc)})
            continue
        all_records.extend(records)
        by_datasource.append(
            {"datasource": ds["name"], "uid": ds["uid"], "query": logql, "occurrences": len(records)}
        )

    clusters = analysis.cluster(all_records)
    obj.emitter.emit(
        {
            "window": window.describe(),
            "line": text,
            "fingerprint": fp,
            "regex": regex,
            "totalOccurrences": len(all_records),
            "byDatasource": by_datasource,
            "clusters": clusters,
        }
    )


def _latest_error_line(client, pool: list[dict], window: TimeRange) -> str:
    """The single most recent error-level line across a pool of datasources.

    Scans every datasource for its own newest error (limit=1 each — cheap) and
    picks the global newest, rather than just the first datasource in the
    pool: seeding `similar` from an arbitrary datasource when several exist
    would silently ignore the one that is actually on fire right now.
    """
    candidates: list[dict] = []
    for ds in pool:
        try:
            logql = _match_all_query(client, ds, window, level="error")
            start_ns, end_ns = window.loki()
            resp = client.ds_proxy(
                ds["uid"], "loki/api/v1/query_range",
                params={"query": logql, "start": start_ns, "end": end_ns, "limit": 1, "direction": "backward"},
            )
            records = loki.parse_streams(resp)
        except OpError:
            continue
        if records:
            candidates.append(records[0])
    if not candidates:
        raise NotFoundError(
            f"no error-level line found across {len(pool)} log datasource(s) in the last "
            f"{window.describe()['seconds']}s to seed --from-last with. Widen --since, or pass a "
            f"line yourself."
        )
    return max(candidates, key=lambda r: r["timestamp"])["line"]


# ---------------------------------------------------------------------------
# levels — where should I look?
# ---------------------------------------------------------------------------


@app.command()
def levels(
    ctx: typer.Context,
    datasource: str = typer.Option(None, "--datasource", "-d", help="Restrict to one datasource. Default: every log datasource."),
    since: str = typer.Option(None, "--since", help="How far back, e.g. 1h, 2d, 30m. Default: your configured default-since."),
    from_: str = typer.Option(None, "--from", help="Explicit window start. Overrides --since."),
    to: str = typer.Option(None, "--to", help="Explicit window end. Default: now."),
    limit: int = typer.Option(None, "--limit", help="Max lines sampled per datasource. Default: your configured default-limit."),
) -> None:
    """The `detected_level` distribution per datasource — where should I look?

    One query per datasource against a match-all selector, then a count of
    `detected_level` values and the top problem clusters (via `analysis.cluster`,
    same engine as `grafana-cli scan`) among them. This is deliberately the cheapest
    possible answer to "where is it worse right now" — one query per source,
    not a query per host or per error category.

    The counts are exact for what was FETCHED, not for the whole window: each
    datasource is sampled up to ``--limit`` lines (newest first), so on a busy
    source with more log volume than ``--limit`` this under-counts older
    levels in the window rather than over-claiming a total nothing here
    actually counted. Narrow the window or raise ``--limit`` for a fuller
    picture; this command trades completeness for being cheap enough to run
    before every other one.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    window = need_window(obj, since=since, start=from_, end=to)
    lim = obj.config.default_limit if limit is None else limit
    pool = _pool(client, datasource)

    report: list[dict] = []
    for ds in pool:
        try:
            logql = _match_all_query(client, ds, window)
            start_ns, end_ns = window.loki()
            resp = client.ds_proxy(
                ds["uid"], "loki/api/v1/query_range",
                params={"query": logql, "start": start_ns, "end": end_ns, "limit": lim, "direction": "backward"},
            )
            records = loki.parse_streams(resp)
        except OpError as exc:
            report.append({"datasource": ds["name"], "uid": ds["uid"], "error": str(exc)})
            continue

        counts = Counter((rec.get("labels") or {}).get("detected_level") or "unknown" for rec in records)
        clusters = [c for c in analysis.cluster(records, top=5) if c.get("severity")]
        report.append(
            {
                "datasource": ds["name"],
                "uid": ds["uid"],
                "sampled": len(records),
                "byLevel": dict(counts.most_common()),
                "topProblems": [
                    {
                        "fingerprint": c["fingerprint"],
                        "category": c["category"],
                        "severity": c["severity"],
                        "count": c["count"],
                    }
                    for c in clusters
                ],
            }
        )

    obj.emitter.emit({"window": window.describe(), "limit": lim, "datasources": report})
