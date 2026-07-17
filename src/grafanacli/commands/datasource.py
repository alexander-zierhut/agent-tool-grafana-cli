"""`graf datasource` — list, inspect, and health-check datasources.

This group is the plain-CRUD complement to :mod:`sources`, which is the pure
discovery engine (`graf logs sources` / `graf metrics sources`). Everything
here calls straight through to `sources.py` rather than re-deriving anything —
the interesting logic (classification, cardinality, the enumerate-and-probe
walk) already lives there and is already tested without a server.

Two things every command here has to respect, both earned live against this
instance:

* **`GET /api/datasources` needs org Admin** (`datasources:read`). An Editor
  token can query a datasource it already knows the uid of, but cannot
  enumerate them — so `list` (and anything built on `sources.list_datasources`)
  is simply unavailable to a lower-privileged token, and the failure has to
  say so precisely rather than surface a bare 403. The client already turns
  that 403 into a message naming the permission; nothing here needs to guess
  at it independently.
* **A datasource's own backend can be down while Grafana is fine.** Verified
  live: one datasource's proxy answers 502 with an EMPTY body. `test` exists
  specifically to make that visible per-datasource without one dead backend
  hiding every healthy one behind it.
"""

from __future__ import annotations

import typer

from .. import sources
from ..errors import NotFoundError
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)

_LIST_COLUMNS = [
    ("uid", "uid"), ("name", "name"), ("type", "type"),
    ("logs", "logs"), ("metrics", "metrics"), ("isDefault", "isDefault"),
]


@app.command("list")
def list_(ctx: typer.Context) -> None:
    """Every datasource in this org, classified by what this CLI can do with it.

    Needs org Admin (`datasources:read`) — see the module docstring. `logs`/
    `metrics` are `"supported"` (this CLI can query it), `"recognised"` (it is
    a known log/metric-capable type, e.g. Elasticsearch, but not implemented
    here yet), or `null` (neither — a plugin datasource, or one that is
    neither a log nor a metric source, e.g. a SQL datasource used only for
    dashboards).
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    rows = [sources.classify(d) for d in sources.list_datasources(client)]
    obj.emitter.emit(rows, columns=_LIST_COLUMNS)


@app.command()
def get(ctx: typer.Context, ref: str = typer.Argument(..., help="Datasource uid or name.")) -> None:
    """Show one datasource's config — resolved by uid or name.

    Grafana exposes uid and name lookup as two different endpoints and gives
    no hint which kind of string you are holding, so this tries uid first
    (`GET /api/datasources/uid/{ref}`) and falls back to name
    (`GET /api/datasources/name/{ref}`) on a 404 — a human reads names off the
    UI, an agent copies uids out of JSON, and refusing either is a papercut.

    **Never prints a secret VALUE.** Grafana's API already enforces this —
    `secureJsonData` (the actual secrets: passwords, tokens, custom headers)
    is write-only and never appears in a GET response; `secureJsonFields` only
    ever carries booleans saying WHICH fields are set. That said, this command
    surfaces those booleans deliberately rather than treating the whole
    section as noise: on this instance one Loki datasource injects a secret
    header (`X-Loki-Label-Preset`) that scopes every query to a tenant, and
    "this datasource has a secret header configured" is exactly the fact
    someone debugging a tenant-scoping problem needs — even though they can
    never see the header's value through this API.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ds = _resolve(client, ref)
    obj.emitter.emit(_redact(ds))


@app.command()
def health(ctx: typer.Context, ref: str = typer.Argument(..., help="Datasource uid or name.")) -> None:
    """Ask Grafana to test one datasource's connectivity.

    Uses `sources.health`, which reports rather than raises: not every
    datasource plugin implements the health-check resource, so a failure here
    can mean "the backend is unreachable" or "this plugin has no health check
    at all" — both are reported as `ok: false` with the server's own message
    rather than one of them crashing the command.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ds = _resolve(client, ref)
    result = sources.health(client, str(ds.get("uid")))
    result["name"] = ds.get("name")
    result["type"] = ds.get("type")
    obj.emitter.emit(result)


@app.command()
def test(ctx: typer.Context) -> None:
    """Health-check EVERY datasource in this org, in one pass.

    Each datasource's failure is captured into its own row, never raised —
    the same contract as `logs sources`/`scan`: one dead backend must not
    blank out the report for every healthy one sitting next to it. Verified
    live: this instance has exactly one datasource whose backend is down
    (a 502 with an empty body through the proxy, which the client maps to
    `DatasourceUnreachable`); it shows up here as one `ok: false` row with the
    reason, beside every other datasource reporting `ok: true`. Nothing about
    a failed health check raises this command's own exit code — read `results`
    (and `healthyCount` vs `datasourceCount`) rather than the process exit.
    """
    obj = ctx_obj(ctx)
    client = obj.client()

    rows = []
    for ds in sources.list_datasources(client):
        result = sources.health(client, str(ds.get("uid")))
        result["name"] = ds.get("name")
        result["type"] = ds.get("type")
        rows.append(result)

    healthy = sum(1 for r in rows if r.get("ok"))
    obj.emitter.emit(
        {
            "datasourceCount": len(rows),
            "healthyCount": healthy,
            "results": rows,
        }
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve(client, ref: str) -> dict:
    """uid first, then name — see `get`'s docstring for why both are tried.

    Only `NotFoundError` is caught on the first attempt: a bad token (401/403)
    or a genuine transport failure must propagate as itself, not be papered
    over as "no such datasource". This is a two-rung resolver, not a
    diagnostic ladder, so it does not end in a catch-all `except OpError` —
    that would swallow exactly the failures that need to reach the caller
    distinctly.
    """
    try:
        return client.get(f"/datasources/uid/{ref}")
    except NotFoundError:
        pass
    try:
        return client.get(f"/datasources/name/{ref}")
    except NotFoundError:
        raise NotFoundError(
            f"no datasource {ref!r} (tried as uid and as name) in this org. "
            f"`graf datasource list` shows what exists."
        ) from None


def _redact(ds: dict) -> dict:
    out = dict(ds)
    # `secureJsonData` never appears in a GET response over this API — secrets
    # are write-only — but stripping it defensively costs nothing and means a
    # future Grafana regression here fails safe instead of leaking a value.
    out.pop("secureJsonData", None)
    secure_fields = ds.get("secureJsonFields") or {}
    out["secureFieldsSet"] = sorted(name for name, is_set in secure_fields.items() if is_set)
    return out
