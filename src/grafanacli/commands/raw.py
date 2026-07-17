"""Escape hatch: call any Grafana API endpoint directly, unwrapped.

Grafana's API is broad and unevenly documented across sub-APIs (see
`client.py`'s module docstring — alerting alone spans four different upstream
projects' shapes under one base URL), so `raw` is how an agent verifies an
endpoint exists at all before believing a doc page. Everything the typed
commands do, `raw` can do — without the derived fields, the safety rails or the
pretty errors.

Paths are relative to ``<server>/api``: ``user`` -> ``<server>/api/user``. A
leading ``/api/`` is tolerated and stripped, because that is what people paste.

**One Grafana-specific trap `raw` does not save you from**: reaching a
datasource's own API (Loki, Prometheus) is not a plain path under `/api` — it is
a tunnel through ``/api/datasources/proxy/uid/{uid}/...``, e.g.::

    graf raw get /datasources/proxy/uid/<uid>/loki/api/v1/labels

A direct connection to Loki itself cannot reproduce this: one org's Loki
datasource injects a secret header (``X-Loki-Label-Preset``) that lives in
Grafana's ``secureJsonFields`` — write-only over the API, so nothing outside
Grafana can ever read it back out and attach it to a direct request. Always go
through the proxy path, even from `raw`.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from typing import Any

import typer

from ..errors import OpError
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)

_PATH_HELP = "API path relative to <server>/api, e.g. user or datasources/proxy/uid/<uid>/loki/api/v1/labels."


def _normalize(path: str) -> str:
    """Accept what people actually paste.

    ``client._url`` joins onto ``<server>/api`` unconditionally, so a path
    copied out of a doc page (``/api/user``) would silently become
    ``/api/api/user`` — a 404 whose body is plain text, not JSON, and which
    reads like the endpoint does not exist. Strip the prefix here rather than
    let that land.
    """
    p = (path or "").strip()
    if p.startswith(("http://", "https://")):
        return p  # an absolute URL is a deliberate choice; leave it alone
    p = p.lstrip("/")
    if p == "api" or p.startswith("api/"):
        p = p[3:].lstrip("/")
    return p


def _params(param: list[str] | None) -> dict:
    out: dict = {}
    for item in param or []:
        if "=" not in item:
            raise OpError(f"--param must be key=value, got {item!r}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def _parse_json(value: str | None, *, what: str) -> Any:
    if value is None:
        return None
    try:
        return jsonlib.loads(value)
    except ValueError as exc:
        raise OpError(f"invalid JSON for {what}: {exc}") from exc


def _body(data: str | None, data_file: Path | None) -> Any:
    if data_file is not None:
        return _parse_json(data_file.read_text(), what="--data-file")
    return _parse_json(data, what="--data")


def _run(ctx: typer.Context, method: str, path: str, param, data=None, data_file=None) -> None:
    obj = ctx_obj(ctx)
    # Writes still go through `client.request` unmodified — that is the ONE
    # chokepoint `--dry-run` is intercepted at, and `raw` bypassing it would be
    # the one command in the whole tree that could not be previewed.
    result = obj.client().request(
        method, _normalize(path), params=_params(param), json=_body(data, data_file)
    )
    obj.emitter.emit(result)


@app.command()
def get(
    ctx: typer.Context,
    path: str = typer.Argument(..., help=_PATH_HELP),
    param: list[str] = typer.Option(None, "--param", "-P", help="Query param key=value (repeatable)."),
) -> None:
    """GET an endpoint and print whatever it returns, unmodified.

    ``/api/health`` is the one Grafana endpoint that answers with no token at
    all — useful for a bare reachability check before touching auth.

    Reads always execute — the global `--dry-run` only suppresses writes.
    """
    _run(ctx, "GET", path, param)


@app.command()
def post(
    ctx: typer.Context,
    path: str = typer.Argument(..., help=_PATH_HELP),
    data: str = typer.Option(None, "--data", "-d", help="JSON request body."),
    data_file: Path = typer.Option(None, "--data-file", help="File containing the JSON body."),
    param: list[str] = typer.Option(None, "--param", "-P", help="Query param key=value (repeatable)."),
) -> None:
    """POST to an endpoint with a JSON body.

    E.g. `POST /dashboards/db` with `--data '{"dashboard": {...}, "folderUid": "..."}'`
    creates or overwrites a dashboard. Preview any write with a global `--dry-run`.
    """
    _run(ctx, "POST", path, param, data, data_file)


@app.command()
def put(
    ctx: typer.Context,
    path: str = typer.Argument(..., help=_PATH_HELP),
    data: str = typer.Option(None, "--data", "-d", help="JSON request body."),
    data_file: Path = typer.Option(None, "--data-file", help="File containing the JSON body."),
    param: list[str] = typer.Option(None, "--param", "-P", help="Query param key=value (repeatable)."),
) -> None:
    """PUT a full JSON body to an endpoint. Preview any write with a global `--dry-run`."""
    _run(ctx, "PUT", path, param, data, data_file)


@app.command()
def patch(
    ctx: typer.Context,
    path: str = typer.Argument(..., help=_PATH_HELP),
    data: str = typer.Option(None, "--data", "-d", help="JSON request body."),
    data_file: Path = typer.Option(None, "--data-file", help="File containing the JSON body."),
    param: list[str] = typer.Option(None, "--param", "-P", help="Query param key=value (repeatable)."),
) -> None:
    """PATCH an endpoint with a partial JSON body. Preview any write with a global `--dry-run`."""
    _run(ctx, "PATCH", path, param, data, data_file)


@app.command()
def delete(
    ctx: typer.Context,
    path: str = typer.Argument(..., help=_PATH_HELP),
    param: list[str] = typer.Option(None, "--param", "-P", help="Query param key=value (repeatable)."),
) -> None:
    """DELETE an endpoint. Usually returns an empty body (-> `null`), not an object."""
    _run(ctx, "DELETE", path, param)
