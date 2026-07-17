"""`graf dashboard` — find, read, and build dashboards.

The commands here fall into two very different jobs, and it is worth being
explicit about which one you are reaching for:

* **`list`/`search`/`get`/`folders`** are ordinary CRUD-adjacent reads.
* **`panels`** is the killer feature, in the same sense `logs sources` is for
  the `logs` group: Grafana will hand you a dashboard's raw JSON if you ask,
  but it will not tell you, anywhere, *what that dashboard actually queries*.
  A dashboard someone else built is opaque until you read `panels[].targets[]`
  yourself and work out which field holds the query and which datasource it
  runs against — two things that vary by datasource type and are not
  documented together anywhere. `panels` does that walk once and hands back
  LogQL/PromQL you can paste straight into `graf logs query -q` or
  `graf metrics query -q`. That turns "here is a dashboard" into "here is what
  it actually looks at", which is the more useful question when you inherited
  a dashboard and want to run its queries yourself, ad hoc, with a different
  window.

**`create`** exists for the opposite direction: you found something querying
logs or metrics directly, and want a dashboard to keep watching it. Building
the minimal valid JSON by hand is enough friction that most people just don't
bother — so `create` does it from a title, a datasource and one or more
queries.

Two Grafana specifics shape both halves:

* **Collapsed rows hide panels.** A row panel that is expanded lists its
  children as ordinary top-level entries in `dashboard.panels[]`; a row that
  is *collapsed* moves them out of the top level entirely and nests them
  under the row's own `panels[]` instead. A flat read of the top level
  therefore silently drops every panel inside a collapsed row — often the
  majority of a dashboard someone tidied up. `panels` walks both shapes.
* **`/api/folders` returns a virtual folder.** Live: `{"id": -1,
  "uid": "sharedwithme"}` sits alongside real folders. It is Grafana's own
  UI grouping for "things shared with me", not a folder you can save into —
  offering it as a `--folder` target for `create` would 404 or worse, silently
  land the dashboard somewhere unexpected. `folders` marks it `virtual: true`
  precisely so nothing downstream mistakes it for a real one.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import typer

from ..errors import OpError, ValidationError
from ._shared import ctx_obj, need_datasource

app = typer.Typer(no_args_is_help=True)

_LIST_COLUMNS = [("uid", "uid"), ("title", "title"), ("folderTitle", "folderTitle"), ("tags", "tags")]
_FOLDER_COLUMNS = [("uid", "uid"), ("title", "title"), ("virtual", "virtual")]

#: `--panel-type` choices for the query-built path. Grafana accepts many more
#: panel type strings than this, but these three are the ones a query alone
#: (no field config, no thresholds) renders sensibly for: a time series, a
#: single number, or raw log lines.
_PANEL_TYPES = ("timeseries", "logs", "stat")

#: Grafana's marker for "this folder is not a folder" — see the module
#: docstring. Checked on `id` too because the live instance returns both.
_VIRTUAL_FOLDER_UID = "sharedwithme"


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


@app.command("list")
def list_(
    ctx: typer.Context,
    folder: str = typer.Option(None, "--folder", help="Folder UID to filter to (see `dashboard folders`)."),
    tag: list[str] = typer.Option(None, "--tag", help="Filter by tag (repeatable; Grafana ANDs multiple tags)."),
    limit: int = typer.Option(None, "--limit", help="Max dashboards (default: the configured default_limit)."),
) -> None:
    """List dashboards — `GET /api/search?type=dash-db`.

    This is the browsing command; if you already have a name in mind, `search`
    reads better. `--folder` takes a UID (`dashboard folders` prints them),
    never a title — Grafana's search API does not accept folder names.

    NOTE: the folder-filter query parameter (`folderUIDs`) is written against
    Grafana's documented modern search API but was not independently exercised
    live against this instance — if it 400s on your server, `--folder` may
    need the older `folderIds` on an install this old.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    n = limit if limit is not None else obj.config.default_limit

    params: dict[str, Any] = {"type": "dash-db"}
    if folder:
        params["folderUIDs"] = folder
    if tag:
        params["tag"] = list(tag)

    rows = list(client.search(params=params, limit=n))
    obj.emitter.emit(rows, columns=_LIST_COLUMNS)


@app.command()
def search(
    ctx: typer.Context,
    term: str = typer.Argument(..., help="Free-text search term (matches dashboard title)."),
    limit: int = typer.Option(None, "--limit", help="Max results (default: the configured default_limit)."),
) -> None:
    """Find dashboards by name — `GET /api/search?query=TERM&type=dash-db`.

    "Find me the dashboard about X" is the question this answers; it is the
    same endpoint as `list` with a `query` term added, split into its own
    command because that is how the question actually gets asked.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    n = limit if limit is not None else obj.config.default_limit
    rows = list(client.search(params={"type": "dash-db", "query": term}, limit=n))
    obj.emitter.emit(rows, columns=_LIST_COLUMNS)


@app.command()
def get(
    ctx: typer.Context,
    uid: str = typer.Argument(..., help="Dashboard UID."),
    out: str = typer.Option(
        None, "--out",
        help="Write the JSON to this file instead of stdout. NOT --output: that flag name is a "
             "reserved global (the output *format*) and is stripped from the command line before "
             "this command ever sees it — a sibling tool shipped exactly that collision for four "
             "releases (the path was silently swallowed as a format, and the write landed in the "
             "wrong place with exit 0).",
    ),
) -> None:
    """Fetch one dashboard — `GET /api/dashboards/uid/{uid}` -> `{dashboard, meta}`.

    The envelope is returned whole, including `meta` (created/updated,
    `folderUid`, `version`, who last saved it) — dropping it would throw away
    exactly the fields `create --overwrite` needs to update this dashboard in
    place later. `--out` writes that same envelope to a file, so
    `dashboard get UID --out d.json` followed by `dashboard create --file
    d.json --overwrite` is a working round trip with no hand-editing of shape.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    payload = client.get(f"/dashboards/uid/{uid}")

    if not out:
        obj.emitter.emit(payload)
        return
    if out == "-":
        # stdout is the machine channel for this command's own JSON envelope;
        # writing the same bytes there under `--out -` would double them, or
        # worse, interleave with the report we print after the write.
        raise OpError(
            "--out - is not supported: stdout already carries this command's JSON envelope. "
            "Write to a real file, or omit --out to print to stdout."
        )
    path = Path(out).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dumps(payload), encoding="utf-8")
    except OSError as exc:
        raise OpError(f"--out {out}: {exc}") from exc

    obj.emitter.emit({"uid": uid, "path": str(path), "action": "written"})


@app.command()
def panels(ctx: typer.Context, uid: str = typer.Argument(..., help="Dashboard UID.")) -> None:
    """What does this dashboard actually query? THE reason this group exists.

    Walks every panel — descending into collapsed rows, see the module
    docstring — and reads each query target's `expr` (LogQL/PromQL),
    `rawSql`, or `target` (legacy Graphite-style), whichever is present,
    alongside the datasource it runs against. Where the datasource type makes
    the query language unambiguous (`loki` -> LogQL, `prometheus` -> PromQL —
    Mimir/Thanos/Cortex all report as `prometheus`), a ready-to-run
    `graf logs query` / `graf metrics query` command is included so you can
    run the same query yourself with a different window, without first
    reverse-engineering which field in the target JSON is the query.

    A query with no obvious language (a `rawSql` panel, or a plugin datasource
    this CLI does not classify) still gets reported — just without a
    `suggested` command, because guessing wrong there would be worse than
    saying nothing.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    payload = client.get(f"/dashboards/uid/{uid}")
    dash = (payload or {}).get("dashboard") or {}

    rows = []
    for p in _iter_panels(dash.get("panels") or []):
        queries = _extract_queries(p)
        rows.append(
            {
                "id": p.get("id"),
                "title": p.get("title"),
                "type": p.get("type"),
                "queryCount": len(queries),
                "queries": queries,
            }
        )

    obj.emitter.emit(
        {
            "uid": uid,
            "title": dash.get("title"),
            "panelCount": len(rows),
            "panels": rows,
        }
    )


@app.command()
def folders(ctx: typer.Context) -> None:
    """List folders — `GET /api/folders`.

    Marks the virtual "Shared with me" folder (`uid: sharedwithme`, live:
    `id: -1`) as `virtual: true`. It is not a save target: it is Grafana's own
    UI grouping, not a real folder, and `create --folder sharedwithme` is a
    trap this command exists to let a caller avoid before hitting it.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    data = client.get("/folders")
    rows = data if isinstance(data, list) else []

    out = []
    for f in rows:
        entry = dict(f)
        entry["virtual"] = f.get("uid") == _VIRTUAL_FOLDER_UID or f.get("id") == -1
        out.append(entry)
    obj.emitter.emit(out, columns=_FOLDER_COLUMNS)


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


@app.command()
def create(
    ctx: typer.Context,
    file: str = typer.Option(
        None, "--file",
        help="A dashboard JSON file to upload as-is. Accepts either a bare dashboard object or the "
             "full {dashboard, meta} envelope `dashboard get` writes — the envelope is unwrapped "
             "automatically, so `get --out d.json` then `create --file d.json --overwrite` round-trips.",
    ),
    title: str = typer.Option(
        None, "--title", help="Dashboard title. Required unless --file supplies one (an explicit "
        "--title with --file renames it)."
    ),
    query: list[str] = typer.Option(
        None, "--query", "-q",
        help="A LogQL/PromQL query to plot (repeatable — each becomes one target/series in a "
             "single panel). Required, at least once, when not using --file.",
    ),
    datasource: str = typer.Option(
        None, "--datasource", "-d",
        help="uid or name of the datasource the queries run against. Query mode only; resolved "
             "the same way as `logs query`/`metrics query` — explicit > sticky context > the only "
             "candidate of the right kind.",
    ),
    panel_type: str = typer.Option(
        "timeseries", "--panel-type", help="timeseries | logs | stat. Query mode only."
    ),
    folder: str = typer.Option(None, "--folder", help="Folder UID to save into (see `dashboard folders`). Omit for General."),
    overwrite: bool = typer.Option(
        False, "--overwrite",
        help="Allow overwriting an existing dashboard. See docstring: without a stable uid "
             "(--file mode only) there is nothing to overwrite, so this mostly matters there.",
    ),
    message: str = typer.Option("", "--message", help="Commit message, shown in the dashboard's version history."),
) -> None:
    """Create a dashboard — `POST /api/dashboards/db`.

    Two ways in:

    * `--file PATH` — upload a dashboard JSON you already have (hand-written,
      exported, or round-tripped from `dashboard get --out`).
    * `--title` + `--query`/`-q` (+ `--datasource`, `--panel-type`) — build the
      minimal valid dashboard from scratch: one panel, one target per `-q`.
      This is the point of the command — "make me a dashboard for the thing I
      just found in the logs" should not require hand-writing panel JSON.

    **Optimistic locking.** Grafana saves are versioned via `dashboard.version`;
    saving over a newer version answers **412**, which this CLI's client maps
    to `ConflictError` (exit 6). `--overwrite` is what you reach for when that
    happens — but it only has something TO overwrite when the dashboard JSON
    already carries the real `uid` it is updating, which in practice means the
    `--file` round trip (`get --out` -> edit -> `create --file --overwrite`).
    The `--title`/`--query` path builds a dashboard with no `uid` at all, so
    each run creates a genuinely new dashboard — there is nothing yet to
    conflict with, and `--overwrite` is a no-op there until you also pass a
    `--file` built from a previous `get`.

    Respects `--dry-run` automatically (the client intercepts every POST
    before it reaches the network) — the request that would be sent, dashboard
    JSON included, is printed instead.
    """
    obj = ctx_obj(ctx)
    client = obj.client()

    if file:
        if query:
            raise ValidationError(
                "--file and --query are mutually exclusive: either upload a dashboard JSON with "
                "--file, or build one from --title/--query, not both."
            )
        dash = _load_file(Path(file).expanduser())
        if title:
            dash["title"] = title
    else:
        if not title:
            raise ValidationError("--title is required when not using --file.")
        if not query:
            raise ValidationError(
                "--query/-q is required at least once when not using --file — pass the "
                "LogQL/PromQL this dashboard should show. Run `graf logs sources` or "
                "`graf metrics sources` first if you do not have one yet."
            )
        if panel_type not in _PANEL_TYPES:
            raise ValidationError(f"--panel-type must be one of {', '.join(_PANEL_TYPES)}, got {panel_type!r}.")
        kind = "logs" if panel_type == "logs" else "metrics"
        ds = need_datasource(obj, datasource, kind=kind)
        dash = _build_dashboard(title, list(query), ds, panel_type)

    body: dict[str, Any] = {"dashboard": dash, "overwrite": overwrite, "message": message or ""}
    if folder:
        body["folderUid"] = folder

    result = client.post("/dashboards/db", json=body)
    obj.emitter.emit(result)


@app.command()
def delete(
    ctx: typer.Context,
    uid: str = typer.Argument(..., help="Dashboard UID to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete a dashboard — `DELETE /api/dashboards/uid/{uid}`. Irreversible.

    No soft-delete, no trash: the dashboard's JSON, its panels and its version
    history are gone. Any alert rule or link pointing at this uid starts
    404ing on the next click.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    if not yes:
        typer.confirm(f"Delete dashboard {uid!r}? This cannot be undone.", abort=True, err=True)
    client.delete(f"/dashboards/uid/{uid}")
    obj.emitter.emit({"status": "deleted", "uid": uid})


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def _dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"


def _load_file(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise OpError(f"--file {path}: {exc}") from exc
    except ValueError as exc:
        raise ValidationError(f"--file {path} is not valid JSON: {exc}") from exc

    if isinstance(raw, dict) and isinstance(raw.get("dashboard"), dict):
        # `dashboard get` writes the full {dashboard, meta} envelope. Unwrapping
        # here is what makes "get, edit, create --overwrite" work without the
        # caller hand-stripping `meta` first.
        return dict(raw["dashboard"])
    if not isinstance(raw, dict):
        raise ValidationError(f"--file {path} must contain a JSON object (a dashboard), not a {type(raw).__name__}.")
    return raw


def _build_dashboard(title: str, queries: list[str], ds: dict, panel_type: str) -> dict:
    """The minimal dashboard JSON Grafana will accept: one panel, N targets.

    `id`/`uid` are explicit `None` rather than omitted, to say plainly "this is
    a new dashboard" — Grafana assigns both on save. `schemaVersion` is left
    out on purpose: guessing a number for "current" invites it going stale the
    moment Grafana ships a new one, and an absent schemaVersion is legal —
    Grafana's migrator treats it as legacy and normalises the shape on save,
    which is a better failure mode than a wrong number silently skipping a
    migration step.
    """
    ds_ref = {"type": ds.get("type"), "uid": ds.get("uid")}
    targets = []
    for i, q in enumerate(queries):
        ref_id = chr(ord("A") + i) if i < 26 else str(i)
        targets.append({"refId": ref_id, "datasource": ds_ref, "expr": q})

    panel = {
        "id": 1,
        "title": title,
        "type": panel_type,
        "datasource": ds_ref,
        "targets": targets,
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": 0},
    }
    return {
        "id": None,
        "uid": None,
        "title": title,
        "panels": [panel],
        "time": {"from": "now-1h", "to": "now"},
        "tags": [],
    }


def _iter_panels(panels_list: list) -> list[dict]:
    """Flatten a dashboard's panel tree, descending into collapsed rows.

    An EXPANDED row's children already live at the top level next to the row
    itself, so recursing into an expanded row's (empty) `panels` adds nothing.
    A COLLAPSED row moves its children OUT of the top level and into its own
    `panels[]` instead — the only place they exist. Reading only the top level
    therefore silently drops every panel inside a collapsed row, which is why
    this recurses rather than iterating once. The row container itself is
    never yielded: it has no queries of its own to report.
    """
    out: list[dict] = []
    for p in panels_list or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "row":
            out.extend(_iter_panels(p.get("panels") or []))
            continue
        out.append(p)
    return out


def _ds_ref(raw: Any) -> dict | None:
    """Normalise a panel/target `datasource` field to `{"type", "uid"}`.

    Modern panels store `{"type": "loki", "uid": "P8E8..."}`; panels exported
    from older Grafanas (or hand-written) sometimes carry just the datasource
    NAME as a bare string. A name cannot be turned into a uid without another
    API call per panel, which `panels` deliberately does not do (it would
    silently turn a single-request command into one request per panel on a
    dashboard with many) — so a bare-string datasource is reported with `uid:
    None`, and the reader resolves it themselves with `graf datasource get
    <name>` if they need to.
    """
    if isinstance(raw, dict):
        return {"type": raw.get("type"), "uid": raw.get("uid")}
    if isinstance(raw, str) and raw:
        return {"type": None, "uid": None, "name": raw}
    return None


def _suggest(ds: dict | None, query_text: Any) -> str | None:
    """A ready-to-run CLI command for one query, only where the language is unambiguous.

    Gated on both a known datasource TYPE (loki/prometheus — the only two this
    CLI queries) and a real uid (a bare-name reference has no uid to pass to
    `--datasource`). Anything else returns None rather than a guess: a wrong
    suggestion that looks confident is worse than an absent one.
    """
    if not ds or not isinstance(query_text, str) or not query_text:
        return None
    uid = ds.get("uid")
    if not uid:
        return None
    kind = (ds.get("type") or "").lower()
    if kind == "loki":
        return f"graf logs query -q {shlex.quote(query_text)} --datasource {uid}"
    if kind == "prometheus":
        return f"graf metrics query -q {shlex.quote(query_text)} --datasource {uid}"
    return None


def _extract_queries(panel: dict) -> list[dict]:
    """Every query a panel runs, with the field it came from and its datasource.

    The field name alone does not say what LANGUAGE a query is: `expr` is used
    by both Loki (LogQL) and Prometheus (PromQL), so the datasource's `type`
    is what actually disambiguates it — carried alongside every query for
    exactly that reason, not as a nicety.
    """
    panel_ds = _ds_ref(panel.get("datasource"))
    out: list[dict] = []
    for target in panel.get("targets") or []:
        if not isinstance(target, dict):
            continue
        ds = _ds_ref(target.get("datasource")) or panel_ds

        if target.get("expr") is not None:
            field, text = "expr", target.get("expr")
        elif target.get("rawSql") is not None:
            field, text = "rawSql", target.get("rawSql")
        elif target.get("target") is not None:
            field, text = "target", target.get("target")
        else:
            field, text = None, None

        out.append(
            {
                "refId": target.get("refId"),
                "queryField": field,
                "query": text,
                "datasource": ds,
                "suggested": _suggest(ds, text),
            }
        )
    return out
