"""`graf alert` — alert rules, and the one question Grafana refuses to answer:
*if this rule fires, who gets told?*

That question is `route`, and it is why this module exists (see :mod:`..routing`
for the join logic). Everything else here is the usual CRUD, with one design
choice threaded through: **`create` never hands back a rule without also
checking whether it can notify anyone.** Creating an alert that fires into a
receiver with zero integrations is not a hypothetical — it is the verified,
live state of the instance this was built against (see `routing`'s docstring),
and every screen in Grafana shows that as healthy. Silence on this point at
creation time is how it stays invisible for months.

Two facts, verified live against Grafana 13.0.3 and not documented anywhere
Grafana publishes, shape several commands below:

* `GET /api/v1/provisioning/alert-rules[/{uid}]` **never returns `folderTitle`**
  — only `folderUID`. But the label Alertmanager actually injects at eval time
  is ``grafana_folder=<the folder's TITLE>``, not the uid (verified on a live
  firing alert: ``grafana_folder: "Platform"``, with the uid
  ``c7k2m9p4q1r8sd`` appearing nowhere in the label set). `routing.rule_labels`
  falls back to the uid when `folderTitle` is absent — which builds a policy
  match against a value Alertmanager never uses. `_folder_title` below closes
  that gap with one extra GET before every routing decision.
* The virtual folder `sharedwithme` (id -1) is not a real folder and is never
  offered as a `create` target (see `spike/VERIFIED_FINDINGS.md`).
"""

from __future__ import annotations

import typer

from .. import routing, sources
from ..errors import ConfigError, NotFoundError, OpError, ValidationError
from ..timerange import parse_duration
from ._shared import ctx_obj, parse_label_args

app = typer.Typer(no_args_is_help=True)

_RULES = "/v1/provisioning/alert-rules"
_POLICIES = "/v1/provisioning/policies"
_RECEIVERS = "/alertmanager/grafana/config/api/v1/receivers"
_FIRING = "/alertmanager/grafana/api/v2/alerts"

_LIST_COLUMNS = ["uid", "title", "folderUID", "for"]
_FIRING_COLUMNS = ["alertname", "state", "since", "receivers"]

# How far back the query stage looks on each evaluation, for `create`. Matches
# the one real provisioned rule on the live instance we could inspect
# (`Example Registry Alert`, uid efhvhftr6yxhce: `relativeTimeRange.from: 600`).
# Not exposed as a flag: independently tuning this AND --for multiplies the
# surface for a query language (LogQL/PromQL) that usually embeds its own
# range-vector duration (`[5m]`) anyway — this is just the outer bound.
_CREATE_LOOKBACK_SECONDS = 600

# `route --exit-code` opts into this band, deliberately far from the 1-10
# taxonomy in errors.py: "nothing would be delivered" is an observed fact
# about Grafana's configuration, not a failure of this CLI (BUILDING.md §9).
# `notify check` shares the exact number on purpose — both report the same
# underlying finding at different scopes (one rule vs. every rule).
EXIT_UNDELIVERED = 20


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _folder_title(client, folder_uid: str | None) -> str | None:
    """Resolve a folder uid to its display title — see the module docstring.

    Best-effort: a dead folder reference must not block a delivery report over
    one failed lookup, so any `OpError` (a 403 on a folder this token cannot
    read, a stale uid, ...) degrades to `None`. `routing.rule_labels` then
    falls back to the uid, which is the wrong label value but *visibly* wrong
    (the report still runs), not silently wrong.
    """
    if not folder_uid:
        return None
    try:
        folder = client.get(f"/folders/{folder_uid}")
    except OpError:
        return None
    return folder.get("title") if isinstance(folder, dict) else None


def _rule_labels_with_folder_title(client, rule: dict) -> dict[str, str]:
    """`routing.rule_labels`, but with the real injected `grafana_folder` value.

    Never mutates the caller's `rule` dict — `create` still needs the original
    server response intact for its own output.
    """
    enriched = dict(rule)
    title = _folder_title(client, rule.get("folderUID"))
    if title:
        enriched["folderTitle"] = title
    return routing.rule_labels(enriched)


# ---------------------------------------------------------------------------
# read surface
# ---------------------------------------------------------------------------


@app.command("list")
def list_rules(
    ctx: typer.Context,
    folder: str = typer.Option(None, "--folder", help="Only rules in this folder UID (exact match — Grafana uids are case-sensitive)."),
    limit: int = typer.Option(None, "--limit", help="Max rules to return. Default: the configured default_limit (100)."),
) -> None:
    """List alert rules — `GET /api/v1/provisioning/alert-rules`.

    This endpoint returns every rule in the org in one response, with no
    pagination and no server-side filter params (verified: the client's own
    docs note the provisioning API "returns everything at once"). So `--folder`
    and `--limit` are both applied client-side, after the fact — cheap here
    because the live instance has a handful of rules, not thousands.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    rows = client.get(_RULES)
    rows = rows if isinstance(rows, list) else []
    if folder:
        rows = [r for r in rows if str(r.get("folderUID")) == folder]
    cap = limit if limit is not None else obj.config.default_limit
    if cap:
        rows = rows[:cap]
    obj.emitter.emit(rows, columns=_LIST_COLUMNS)


@app.command("get")
def get_rule(
    ctx: typer.Context,
    uid: str = typer.Argument(..., help="Alert rule UID."),
) -> None:
    """Show one alert rule — `GET /api/v1/provisioning/alert-rules/{uid}`.

    This is the raw provisioning shape: `data[]` carries the query stages
    verbatim, including the `__expr__` reduce/threshold stage that most UIs
    hide. Use `route {uid}` for "who does this actually reach" — this command
    only shows what the rule IS, not what it DOES.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    rule = client.get(f"{_RULES}/{uid}")
    obj.emitter.emit(rule)


@app.command("firing")
def firing(
    ctx: typer.Context,
    active: bool = typer.Option(None, "--active/--no-active", help="Only (or exclude) alerts in the 'active' state."),
    silenced: bool = typer.Option(None, "--silenced/--no-silenced", help="Only (or exclude) silenced alerts."),
    inhibited: bool = typer.Option(None, "--inhibited/--no-inhibited", help="Only (or exclude) inhibited alerts."),
) -> None:
    """Currently firing instances — `GET /api/alertmanager/grafana/api/v2/alerts`.

    Shows which receiver(s) each instance is *currently* routed to (the API
    returns this directly, no join needed) — but NOT whether that receiver can
    actually deliver. A receiver name here can be the empty-integrations trap
    this whole module exists to catch; cross-check with
    `graf notify check` or `graf alert route <rule-uid>` before trusting that
    "routed to X" means "reached someone".

    The three filters are the standard Alertmanager v2 query params
    (`active`/`silenced`/`inhibited`, each tri-state: omit to include both).
    They are documented upstream Alertmanager API, not independently
    re-verified against this instance with every combination — flag if one
    behaves unexpectedly.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    params: dict = {}
    if active is not None:
        params["active"] = active
    if silenced is not None:
        params["silenced"] = silenced
    if inhibited is not None:
        params["inhibited"] = inhibited
    raw = client.get(_FIRING, params=params or None)
    rows = raw if isinstance(raw, list) else []
    obj.emitter.emit([_decorate_firing(a) for a in rows], columns=_FIRING_COLUMNS)


def _decorate_firing(a: dict) -> dict:
    labels = a.get("labels") or {}
    status = a.get("status") or {}
    receivers = [str(r.get("name")) for r in (a.get("receivers") or []) if isinstance(r, dict)]
    return {
        "alertname": labels.get("alertname"),
        "state": status.get("state"),
        "since": a.get("startsAt"),
        "receivers": receivers,
        "labels": labels,
        "silencedBy": status.get("silencedBy") or [],
        "inhibitedBy": status.get("inhibitedBy") or [],
        "fingerprint": a.get("fingerprint"),
        "generatorURL": a.get("generatorURL"),
    }


@app.command("route")
def route(
    ctx: typer.Context,
    uid: str = typer.Argument(None, help="Alert rule UID. Omit and use --label to route a hypothetical label set with no rule at all."),
    label: list[str] = typer.Option(
        None, "--label",
        help="k=v (repeatable). With no UID: route this hypothetical label set — 'what if an alert with severity=critical fired?' "
             "With a UID: merged onto that rule's own labels — 'what if THIS rule also carried severity=critical?'",
    ),
    exit_code: bool = typer.Option(
        False, "--exit-code",
        help=f"Exit {EXIT_UNDELIVERED} if nothing would be delivered. Default: always exit 0 — an "
             f"undeliverable route is an observed fact about Grafana's config, not a failure of this CLI.",
    ),
) -> None:
    """If this rule fires, who gets told? THE reason this module exists.

    Grafana gives you the rule, the policy tree and the receivers as three
    separate objects and joins none of them (see `routing.delivery_report`).
    An alert can fire forever into a receiver with zero integrations and every
    screen in the Grafana UI will still show it "firing" — never "undelivered",
    because Grafana has no such state. This command computes it.

    Pass a rule UID for the real question, or `--label` alone for a
    hypothetical one — worth answering *before* you write the rule, not after
    it has been silently firing into the void for a month:

        graf alert route efhvhftr6yxhce
        graf alert route --label severity=critical --label team=payments
        graf alert route efhvhftr6yxhce --label severity=critical   # tweak one rule's labels

    Never raises for "undelivered" — that is the finding, not an error. Gate a
    non-zero exit behind `--exit-code` if a script needs to branch on it.
    """
    obj = ctx_obj(ctx)
    client = obj.client()

    if uid is None and not label:
        raise ValidationError(
            "pass a rule UID, or --label k=v (repeatable) to route a hypothetical label set."
        )

    extra = parse_label_args(label)
    if uid is not None:
        rule = client.get(f"{_RULES}/{uid}")
        labels = _rule_labels_with_folder_title(client, rule)
        labels.update(extra)
        rule_title = rule.get("title")
    else:
        labels = extra
        rule_title = None

    tree = client.get(_POLICIES)
    tree = tree if isinstance(tree, dict) else {}
    receivers = client.get(_RECEIVERS)
    receivers = receivers if isinstance(receivers, list) else []

    report = routing.delivery_report(labels, tree, receivers, rule=rule_title)
    if uid is not None:
        report["ruleUID"] = uid
    else:
        report["hypothetical"] = True
    obj.emitter.emit(report)

    if exit_code and not report["delivered"]:
        raise typer.Exit(code=EXIT_UNDELIVERED)


# ---------------------------------------------------------------------------
# write surface
# ---------------------------------------------------------------------------


def _resolve_alert_datasource(client, ref: str | None) -> dict:
    """Datasource resolution for `create`, across BOTH logs and metrics.

    `sources.resolve` (used everywhere else in this CLI) takes a single `kind`
    up front, because every other command already knows whether it wants logs
    or metrics. `create` does not: `--query` can be LogQL or PromQL, and the
    datasource's type is the only thing that tells them apart. So this pools
    `sources.log_datasources` and `sources.metric_datasources` and resolves
    against the union — reusing their classification, not reimplementing it.
    """
    pool: dict[str, dict] = {d["uid"]: d for d in sources.log_datasources(client)}
    for d in sources.metric_datasources(client):
        pool.setdefault(d["uid"], d)
    supported = [d for d in pool.values() if d.get("logs") == "supported" or d.get("metrics") == "supported"]

    if ref:
        for d in pool.values():
            if d["uid"] == ref:
                return d
        matches = [d for d in pool.values() if str(d["name"]).lower() == ref.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            uids = ", ".join(str(d["uid"]) for d in matches)
            raise ConfigError(f"{ref!r} matches {len(matches)} datasources ({uids}). Pass the uid instead of the name.")
        known = ", ".join(f"{d['name']} ({d['uid']})" for d in pool.values()) or "none"
        raise NotFoundError(f"no datasource {ref!r} in this org. Available: {known}.")

    if not supported:
        raise ConfigError(
            "no Loki or Prometheus/Mimir datasource in this org — `create` only knows how to "
            "build a rule body for those two types. `graf datasource list` shows what exists."
        )
    if len(supported) == 1:
        return supported[0]
    names = ", ".join(f"{d['name']} ({d['uid']}, {d['type']})" for d in supported)
    raise ConfigError(f"this org has {len(supported)} datasources this CLI can alert on: {names}. Pick one with --datasource.")


def _writable_folders(client) -> list[dict]:
    """Real folders this token can plausibly create alert rules in.

    Two sources exist and neither is trustworthy alone:

    * `GET /api/folders` carries no permission info at all — just id/uid/title.
    * `/api/access-control/user/permissions` is proven unreliable here: live,
      `alert.rules:create` lists BOTH `folders:*` (a wildcard scope from a
      broader role) AND the one folder this token can actually write to — and
      an attempted create into any OTHER folder still 403s. Presence of a
      permission scope is not capability (see `VERIFIED_FINDINGS.md`).

    `GET /api/folders/{uid}` per folder returns `canSave`, which is what
    Grafana's OWN UI gates the "new alert rule" affordance on — the closest
    thing to ground truth available without attempting a write. The virtual
    `sharedwithme` folder (id -1) is always excluded; it is not a real folder
    and a create into it 404s.
    """
    out: list[dict] = []
    for f in client.get("/folders") or []:
        uid = f.get("uid")
        if uid == "sharedwithme":
            continue
        try:
            detail = client.get(f"/folders/{uid}")
        except OpError:
            out.append({**f, "canSave": None})
            continue
        out.append({**f, "canSave": detail.get("canSave") if isinstance(detail, dict) else None})
    return out


def _query_stage(ds: dict, query: str) -> dict:
    """The `A` stage: the actual LogQL/PromQL query, evaluated as an instant value.

    Grafana alerting needs one scalar per evaluation, not raw log lines or a
    range-vector series — so `query` must already reduce to a number (e.g.
    `count_over_time({app="foo"} |= "error" [5m])` for Loki, or a bare PromQL
    expression for Prometheus/Mimir). `create` does not validate this; a raw
    log selector is accepted here and rejected server-side with a shape
    Grafana explains, not this CLI. Modeled field-for-field on a real
    provisioned rule (`Example Registry Alert`, uid efhvhftr6yxhce).
    """
    return {
        "refId": "A",
        "queryType": "instant",
        "relativeTimeRange": {"from": _CREATE_LOOKBACK_SECONDS, "to": 0},
        "datasourceUid": ds["uid"],
        "model": {
            "datasource": {"type": ds["type"], "uid": ds["uid"]},
            "editorMode": "code",
            "expr": query,
            "hide": False,
            "intervalMs": 1000,
            "maxDataPoints": 43200,
            "queryType": "instant",
            "refId": "A",
        },
    }


def _threshold_stage(ref_id: str, source_ref: str, condition: str, threshold: float) -> dict:
    """The reduce+threshold stage against Grafana's built-in `__expr__` "datasource".

    Grafana's `threshold` expression type does last-value reduction AND the
    comparison in one stage (no separate `reduce` stage needed when the input
    is already a single instant value) — verified live, same source rule as
    `_query_stage`. `condition` is the evaluator TYPE (Grafana overloads
    "condition" itself for two different things: this evaluator, and the
    rule-level field naming WHICH stage is the pass/fail one — see `create`'s
    docstring). Only `"gte"` is confirmed against this instance; `"gt"`/`"lt"`/
    `"lte"` are the documented Grafana threshold operators but not
    independently re-verified here.
    """
    return {
        "refId": ref_id,
        "queryType": "",
        "relativeTimeRange": {"from": 0, "to": 0},
        "datasourceUid": "__expr__",
        "model": {
            "conditions": [
                {
                    "evaluator": {"params": [threshold], "type": condition},
                    "operator": {"type": "and"},
                    "query": {"params": []},
                    "reducer": {"params": [], "type": "last"},
                    "type": "query",
                }
            ],
            "datasource": {"type": "__expr__", "uid": "__expr__"},
            "expression": source_ref,
            "intervalMs": 1000,
            "maxDataPoints": 43200,
            "refId": ref_id,
            "type": "threshold",
        },
    }


@app.command("create")
def create(
    ctx: typer.Context,
    title: str = typer.Option(..., "--title", help="Rule title."),
    datasource: str = typer.Option(None, "--datasource", "-d", help="Datasource uid or name. Required if the org has more than one Loki/Prometheus datasource."),
    query: str = typer.Option(..., "--query", "-q", help="LogQL or PromQL. Must reduce to a single scalar per evaluation (e.g. count_over_time(...) for Loki), not raw log lines."),
    condition: str = typer.Option("gte", "--condition", help="Threshold evaluator: gte (verified live), gt/lt/lte (documented, not independently verified here)."),
    threshold: float = typer.Option(0.0, "--threshold", help="Threshold value. Default 0.0 + gte means: fire the moment the query returns anything at all — 'let me know if this happens again'."),
    folder: str = typer.Option(None, "--folder", help="Folder UID. Required unless exactly one real folder is writable (`sharedwithme` never counts)."),
    group: str = typer.Option("graf", "--group", help="Rule group name."),
    for_: str = typer.Option("5m", "--for", help="Pending period before a firing condition actually fires, e.g. 5m, 1h."),
    label: list[str] = typer.Option(None, "--label", help="k=v (repeatable). THESE are what the notification policy tree matches on — check with `route` before relying on one."),
    annotation: list[str] = typer.Option(None, "--annotation", help="k=v (repeatable). Free-form context on the firing alert; never used for routing."),
    summary: str = typer.Option(None, "--summary", help="Shorthand for --annotation summary=...; wins over an explicit --annotation summary=."),
) -> None:
    """Create an alert rule from a query — and immediately say who it can reach.

    POSTs to `/api/v1/provisioning/alert-rules`. The body has two `data[]`
    stages modeled on a real provisioned rule inspected live (see
    `_query_stage`/`_threshold_stage`): your query as an instant value (`A`),
    then a `threshold` expression (`B`) that reduces and compares it. The
    rule's top-level `condition` field is set to `"B"` — Grafana's name for
    "which stage is the pass/fail one", a different thing from the evaluator
    `--condition` above despite the shared word.

    `notification_settings` is deliberately OMITTED from the body — leaving it
    unset routes the new rule through the notification policy tree by label,
    which is the whole point of a tool built around `alert route`. Pinning it
    to one receiver here would silently bypass that.

    **Every read below (org id, folder discovery, datasource resolution) still
    executes under `--dry-run`** — only the final `client.post` is intercepted,
    by the transport, so the printed body is the real one, not a guess.

    After a real (non-dry-run) create, this runs `route`'s own join
    (`routing.delivery_report`) against the new rule's labels and folds the
    result into the output under `delivery`, with a `warning` field if nothing
    would actually be delivered. Creating an alert that cannot notify anyone
    is the exact failure this tool exists to prevent — this must say so at
    creation time, not months later when a whole fleet of rules are firing
    into a receiver with zero integrations and every Grafana screen looks fine
    (see `routing`'s module docstring for the live instance that motivated this).

    UNVERIFIED against a real create (this was built without writing to the
    live instance): the exact acceptance of an omitted `notification_settings`
    and of evaluator types other than `"gte"`. Both degrade honestly — a
    rejected shape comes back as a `ValidationError` naming what the server
    did not like, not a silent wrong rule.
    """
    obj = ctx_obj(ctx)
    client = obj.client()

    ds = _resolve_alert_datasource(client, datasource)

    # Fail fast on a typo'd duration before any write is attempted; the
    # ORIGINAL string still goes on the wire — Grafana wants "5m", not seconds.
    parse_duration(for_)

    if folder:
        folder_uid = folder
        folder_auto_selected = False
    else:
        try:
            candidates = [f for f in _writable_folders(client) if f.get("canSave") is not False]
        except OpError as exc:
            raise ConfigError(f"could not enumerate folders to auto-pick one ({exc}). Pass --folder <uid>.") from exc
        if not candidates:
            raise ConfigError("no writable folder found (excluding the virtual 'sharedwithme'). Pass --folder <uid>.")
        if len(candidates) > 1:
            names = ", ".join(f"{f['title']} ({f['uid']})" for f in candidates)
            raise ConfigError(f"{len(candidates)} folders available: {names}. Pick one with --folder.")
        folder_uid = candidates[0]["uid"]
        folder_auto_selected = True

    org = client.get("/org")
    org_id = org.get("id") if isinstance(org, dict) else None

    labels = parse_label_args(label)
    annotations = parse_label_args(annotation)
    if summary:
        annotations["summary"] = summary

    body = {
        "orgID": org_id,
        "folderUID": folder_uid,
        "ruleGroup": group,
        "title": title,
        "condition": "B",
        "data": [_query_stage(ds, query), _threshold_stage("B", "A", condition, threshold)],
        "noDataState": "NoData",
        "execErrState": "Error",
        "for": for_,
        "labels": labels,
        "annotations": annotations,
        "isPaused": False,
    }

    created = client.post(_RULES, json=body)

    out = dict(created) if isinstance(created, dict) else {"title": title, "folderUID": folder_uid}
    if folder_auto_selected:
        out["folderAutoSelected"] = True

    delivery_labels = _rule_labels_with_folder_title(client, out)
    tree = client.get(_POLICIES)
    tree = tree if isinstance(tree, dict) else {}
    receivers = client.get(_RECEIVERS)
    receivers = receivers if isinstance(receivers, list) else []
    delivery = routing.delivery_report(delivery_labels, tree, receivers, rule=out.get("title"))
    out["delivery"] = delivery
    if not delivery["delivered"]:
        out["warning"] = "created, but will not notify anyone: " + "; ".join(delivery["problems"])

    obj.emitter.emit(out)


@app.command("delete")
def delete_rule(
    ctx: typer.Context,
    uid: str = typer.Argument(..., help="Alert rule UID to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation. Required when not on a TTY."),
) -> None:
    """Delete an alert rule. Irreversible — its evaluation state is gone.

    (Notifications Grafana already SENT for it are not recalled; only future
    evaluation stops.)
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    if not yes:
        if not obj.interactive:
            raise OpError(f"refusing to delete rule {uid!r} without confirmation. Pass --yes.")
        typer.confirm(f"Delete alert rule {uid!r}? This cannot be undone.", abort=True, err=True)
    client.delete(f"{_RULES}/{uid}")
    obj.emitter.emit({"status": "deleted", "uid": uid})


def _set_paused(ctx: typer.Context, uid: str, paused: bool) -> None:
    """Fetch-flip-PUT-back — see `pause`/`unpause` docstrings for why this shape
    is a deliberate, documented guess rather than a verified one."""
    obj = ctx_obj(ctx)
    client = obj.client()
    rule = client.get(f"{_RULES}/{uid}")
    if not isinstance(rule, dict):
        raise NotFoundError(f"no alert rule {uid!r}.")
    rule["isPaused"] = paused
    updated = client.put(f"{_RULES}/{uid}", json=rule)
    obj.emitter.emit(updated if isinstance(updated, dict) else {"uid": uid, "isPaused": paused})


@app.command("pause")
def pause(ctx: typer.Context, uid: str = typer.Argument(..., help="Alert rule UID.")) -> None:
    """Pause an alert rule — stop evaluating it without deleting it.

    UNVERIFIED SHAPE: this was built without a live write to avoid mutating a
    real, currently-alerting instance. `PUT /api/v1/provisioning/alert-rules/{uid}`
    replaces the whole rule (that much is the documented provisioning-API
    contract); this fetches the rule GET already proved valid, flips only
    `isPaused`, and PUTs the same object back — the smallest possible change to
    a body already known to be acceptable, rather than constructing a partial
    body from scratch. If this 400s, the shape assumption is wrong: report it.
    """
    _set_paused(ctx, uid, True)


@app.command("unpause")
def unpause(ctx: typer.Context, uid: str = typer.Argument(..., help="Alert rule UID.")) -> None:
    """Resume a paused alert rule. Same unverified-shape caveat as `pause`."""
    _set_paused(ctx, uid, False)
