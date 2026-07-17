"""`grafana-cli notify` — contact points, the policy tree, and the fleet-wide audit.

Where `alert route` answers "who does THIS rule reach", `notify check` answers
it for every rule at once — the "is my alerting actually wired up?" command.
Both share the same underlying join (`routing.delivery_report`) and the same
`--exit-code` discipline: an undeliverable route is a fact about Grafana's
configuration, not a failure of this CLI, so it never raises on its own.

`notify list` exists because of one specific, verified disagreement: Grafana
exposes contact points through two unrelated APIs that do not agree with each
other. `GET /api/v1/provisioning/contact-points` returned `[]` on the live
instance; `GET /api/alertmanager/grafana/config/api/v1/receivers` returned a
receiver named `Default` with `integrations: []` — present, and functionally
empty. Reading either alone tells half the truth: provisioning would report no
contact points exist at all (wrong — one does, it just can't deliver), and the
alertmanager view alone would miss anything defined but not yet live. Read
both, always.
"""

from __future__ import annotations

import typer

from .. import routing
from ..errors import NotFoundError, OpError, ValidationError
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)

_CONTACT_POINTS = "/v1/provisioning/contact-points"
_RECEIVERS = "/alertmanager/grafana/config/api/v1/receivers"
_POLICIES = "/v1/provisioning/policies"
_RULES = "/v1/provisioning/alert-rules"
_SILENCES = "/alertmanager/grafana/api/v2/silences"

# Shares its value and its meaning with `alert.EXIT_UNDELIVERED` on purpose:
# both report "at least one thing would not be delivered", just at different
# scope (one hypothetical route vs. every provisioned rule). See BUILDING.md
# §9 — this band is for observed facts, never renumbered, never reused for a
# CLI failure.
EXIT_UNDELIVERED = 20


def _folder_title(client, folder_uid: str | None) -> str | None:
    """Resolve a folder uid to its display title.

    Duplicated (not imported) from `alert.py`'s identical helper: command
    modules in this tool are self-contained by convention, since each is owned
    and replaceable independently. See `alert.py`'s module docstring for why
    this matters — the provisioning alert-rules API never returns
    `folderTitle`, only `folderUID`, while Alertmanager's real `grafana_folder`
    label is the folder's TITLE (verified live). Every rule fed through
    `routing.rule_labels` in this module goes through this first, or `check`
    would silently mis-route by folder for anyone whose policy tree branches
    on `grafana_folder`.
    """
    if not folder_uid:
        return None
    try:
        folder = client.get(f"/folders/{folder_uid}")
    except OpError:
        return None
    return folder.get("title") if isinstance(folder, dict) else None


@app.command("list")
def list_contact_points(ctx: typer.Context) -> None:
    """Contact points — merged from BOTH alerting APIs, because they disagree.

    See the module docstring for the live disagreement this reads around. The
    merge priority is deliberate: a name's integrations come from the
    alertmanager view first (that is what actually dispatches), falling back
    to the provisioning view only if the alertmanager side reported none —
    covering a contact point defined via provisioning that has not reached the
    active config yet.

    `usable: false` is the point of this command: a contact point can exist,
    have a name, appear in `grafana-cli alert route`'s output as a real receiver, and
    still deliver to nobody. Every screen in Grafana's own UI shows that case
    as configured and healthy.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    provisioned = client.get(_CONTACT_POINTS)
    provisioned = provisioned if isinstance(provisioned, list) else []
    receivers = client.get(_RECEIVERS)
    receivers = receivers if isinstance(receivers, list) else []

    by_name: dict[str, dict] = {}
    for cp in provisioned:
        name = str(cp.get("name"))
        entry = by_name.setdefault(name, {"name": name, "seenIn": set(), "provisioning": [], "alertmanager": None})
        entry["seenIn"].add("provisioning")
        entry["provisioning"].append(cp)
    for r in receivers:
        name = str(r.get("name"))
        entry = by_name.setdefault(name, {"name": name, "seenIn": set(), "provisioning": [], "alertmanager": None})
        entry["seenIn"].add("alertmanager")
        entry["alertmanager"] = r

    out = []
    for name, entry in by_name.items():
        integrations = routing.integration_names(entry["alertmanager"] or {})
        if not integrations:
            for cp in entry["provisioning"]:
                integrations.extend(routing.integration_names(cp))
        usable = bool(integrations)

        problems = []
        if not usable:
            problems.append(
                f"contact point {name!r} exists but configures zero integrations "
                f"(no email, no webhook, nothing) — anything routed here fires into the void."
            )
        if "alertmanager" not in entry["seenIn"]:
            # The alertmanager config is what actually dispatches; a name that
            # never reached it cannot deliver regardless of what provisioning says.
            problems.append(
                f"{name!r} is defined via the provisioning API but does not appear in the "
                f"active alertmanager config — it may not be live yet."
            )

        out.append({
            "name": name,
            "seenIn": sorted(entry["seenIn"]),
            "integrations": integrations,
            "usable": usable and "alertmanager" in entry["seenIn"],
            "problem": "; ".join(problems) or None,
        })

    out.sort(key=lambda e: (e["usable"], e["name"]))
    obj.emitter.emit(out, columns=["name", "usable", "integrations", "seenIn", "problem"])


def _route_label(node: dict) -> str:
    """A human-readable name for one route, for the flattened `policies` view.

    Deliberately re-derived here rather than reaching into `routing._label_of`
    — that name is prefixed private on purpose (internal to how
    `resolve_receivers` builds its match path), and this module only depends
    on `routing`'s public surface (`parse_matchers`, `integration_names`,
    `delivery_report`, `rule_labels`).
    """
    matchers = routing.parse_matchers(node)
    if matchers:
        return " & ".join(m.describe() for m in matchers)
    return str(node.get("receiver") or "<route>")


def _flatten_tree(node: dict, *, path: list[str], inherited: str | None) -> list[dict]:
    """Every route in the tree, matched or not — an inventory, not a lookup.

    Distinct from `routing.resolve_receivers`, which walks the tree for ONE
    label set and stops at the first match. `policies` answers a different
    question — "what does this tree contain" — so it visits every branch
    regardless of whether anything would ever hit it.
    """
    receiver = node.get("receiver") or inherited
    row = {
        "path": " > ".join(path) if path else "<root>",
        "receiver": receiver,
        "matchers": [m.describe() for m in routing.parse_matchers(node)],
        "continue": bool(node.get("continue")),
        "muteTimeIntervals": list(node.get("mute_time_intervals") or []),
        "groupBy": list(node.get("group_by") or []),
    }
    out = [row]
    for child in node.get("routes") or []:
        if not isinstance(child, dict):
            continue
        out.extend(_flatten_tree(child, path=path + [_route_label(child)], inherited=receiver))
    return out


@app.command("policies")
def policies(
    ctx: typer.Context,
    tree_view: bool = typer.Option(False, "--tree", help="Raw nested policy tree, as Grafana stores it, instead of the flattened path table."),
) -> None:
    """The notification policy tree — `GET /api/v1/provisioning/policies`.

    Default output is a flat table (path -> receiver -> matchers): the raw
    tree nests routes inside routes inside routes, and reading label-based
    routing out of that nesting by eye is exactly the kind of assembly this
    whole tool exists to do instead of you. `--tree` gives the real nested
    object back for anyone who wants to feed it into something else.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    tree = client.get(_POLICIES)
    tree = tree if isinstance(tree, dict) else {}
    if tree_view:
        obj.emitter.emit(tree)
        return
    rows = _flatten_tree(tree, path=[], inherited=None)
    obj.emitter.emit(rows, columns=["path", "receiver", "matchers", "continue", "muteTimeIntervals"])


@app.command("check")
def check(
    ctx: typer.Context,
    exit_code: bool = typer.Option(
        False, "--exit-code",
        help=f"Exit {EXIT_UNDELIVERED} if ANY rule would not be delivered. Default: always exit 0.",
    ),
) -> None:
    """Audit EVERY alert rule's delivery in one pass — is alerting actually wired up?

    Runs `routing.rule_labels` -> `routing.delivery_report` for each rule
    returned by `GET /api/v1/provisioning/alert-rules`, against one shared
    fetch of the policy tree and receivers (not one fetch per rule — the tree
    and receiver list are the same for every rule in an org, so this is O(1)
    network calls beyond O(rules)). Folder-title lookups are cached per
    `folderUID` for the same reason: several rules typically share a folder.

    This is the command to run after touching alerting config at all, or on a
    schedule: it is the only way to learn "3 of 12 rules fire into a receiver
    with no integrations" in one call instead of re-deriving it per rule.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    rules = client.get(_RULES)
    rules = rules if isinstance(rules, list) else []
    tree = client.get(_POLICIES)
    tree = tree if isinstance(tree, dict) else {}
    receivers = client.get(_RECEIVERS)
    receivers = receivers if isinstance(receivers, list) else []

    folder_titles: dict[str, str | None] = {}
    reports = []
    for rule in rules:
        folder_uid = rule.get("folderUID")
        if folder_uid not in folder_titles:
            folder_titles[folder_uid] = _folder_title(client, folder_uid)
        enriched = dict(rule)
        if folder_titles.get(folder_uid):
            enriched["folderTitle"] = folder_titles[folder_uid]
        labels = routing.rule_labels(enriched)
        report = routing.delivery_report(labels, tree, receivers, rule=rule.get("title"))
        report["ruleUID"] = rule.get("uid")
        reports.append(report)

    broken = [r for r in reports if not r["delivered"]]
    out = {
        "rules": reports,
        "totalRules": len(reports),
        "undeliverable": len(broken),
        "problems": [
            {"rule": r.get("rule"), "ruleUID": r.get("ruleUID"), "problems": r["problems"]}
            for r in broken
        ],
    }
    obj.emitter.emit(out)

    if exit_code and broken:
        raise typer.Exit(code=EXIT_UNDELIVERED)


# `notify test` USED TO LIVE HERE, and it is deliberately gone.
#
# Grafana 13.0.3 answers the documented endpoint with **410 Gone**:
#
#   POST /api/alertmanager/grafana/config/api/v1/receivers/test
#   -> 410 {"message":"This endpoint has been removed. Please use
#           `/apis/notifications.alerting.grafana.app/v1beta1/namespaces/
#            {namespace}/receivers/{uid}/test` instead."}
#
# The replacement is reachable (namespace `default` = the current org, receiver
# uids are base64 of the title), but it rejected every body shape tried --
# empty, an alert payload, the receiver read straight back from the API, and a
# hand-built spec -- always with `400 Invalid receiver: 'unknown integration
# type: '`. The settings are NOT redacted on read, so that is not the cause; the
# envelope it wants was not worked out. All measured against the throwaway stack.
#
# So there is no command, rather than a command that always 400s. A CLI that
# ships a verb which cannot work is worse than one that admits the gap: the
# first costs you a debugging session, the second costs you a sentence.
#
# It is also the least missed thing here. "Will this alert reach me?" is what
# people actually want, and `alert route` / `notify check` answer it exactly,
# statically, without dispatching anything at a real inbox. Sending a test
# notification only proves one integration works right now; the routing report
# proves the whole path from rule labels to a receiver that can deliver.
#
# To resurrect it: work out the v1beta1 envelope against `make stack`, then add
# a live test. Do not re-add it on a guess -- that is how it got removed.

@app.command("silences")
def silences(ctx: typer.Context) -> None:
    """List silences — `GET /api/alertmanager/grafana/api/v2/silences`.

    A silence looks like resolution from every other Grafana screen: the alert
    stops paging. `state` distinguishes `active` from `expired`/`pending` — an
    expired silence with a stale `endsAt` is easy to mistake for a live one at
    a glance, which is exactly the kind of gap `alert route`'s `problems` field
    calls out for a currently-muted route.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    rows = client.get(_SILENCES)
    rows = rows if isinstance(rows, list) else []
    obj.emitter.emit(
        [_decorate_silence(s) for s in rows],
        columns=["id", "state", "matchers", "startsAt", "endsAt", "comment", "createdBy"],
    )


def _decorate_silence(s: dict) -> dict:
    matchers = [m for m in (s.get("matchers") or []) if isinstance(m, dict)]
    return {
        "id": s.get("id"),
        "state": (s.get("status") or {}).get("state"),
        "matchers": [_describe_silence_matcher(m) for m in matchers],
        "startsAt": s.get("startsAt"),
        "endsAt": s.get("endsAt"),
        "comment": s.get("comment"),
        "createdBy": s.get("createdBy"),
    }


def _describe_silence_matcher(m: dict) -> str:
    negate = m.get("isEqual") is False
    op = ("!~" if negate else "=~") if m.get("isRegex") else ("!=" if negate else "=")
    return f"{m.get('name')}{op}{m.get('value')!r}"
