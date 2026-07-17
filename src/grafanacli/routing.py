"""Notification routing — will this alert actually reach a human?

Pure logic, no HTTP. This module is the answer to the one question Grafana will
not answer anywhere, in the UI or the API: *if this rule fires, who gets told?*

Grafana gives you the pieces and no assembly:

* the **rule** knows its labels;
* the **notification policy tree** maps labels -> a receiver, by Alertmanager's
  first-match-wins-unless-continue walk;
* the **receiver** holds the integrations that actually deliver.

Nothing joins them. So an alert can fire forever into a receiver with zero
integrations and every screen in Grafana will show it working. That is not
hypothetical — it is the live state of the instance this was built against: nine
alerts firing, all routed to a receiver named ``Default`` whose ``integrations``
array is empty. `delivery_report` exists to say that out loud.

The walk below is a faithful port of Alertmanager's ``Route.Match``
(``dispatch/route.go``), because a *nearly* correct routing model is worse than
none: it would confidently name the wrong receiver.
"""

from __future__ import annotations

import re
from typing import Any

#: Alertmanager matcher operators, longest-first so `=~` never parses as `=`.
_OPS = ("=~", "!~", "!=", "=")


class Matcher:
    """One ``label <op> value`` test."""

    __slots__ = ("name", "op", "value", "_rx")

    def __init__(self, name: str, op: str, value: str) -> None:
        if op not in _OPS:
            raise ValueError(f"unknown matcher operator {op!r}")
        self.name = name
        self.op = op
        self.value = value
        self._rx: re.Pattern | None = None
        if op in ("=~", "!~"):
            # Alertmanager anchors regex matchers at both ends; an unanchored
            # port would match far too much and name the wrong receiver.
            try:
                self._rx = re.compile(rf"^(?:{value})$")
            except re.error:
                self._rx = None

    def matches(self, labels: dict[str, str]) -> bool:
        """Test against a label set.

        A missing label is the empty string — Alertmanager's rule, and the reason
        ``severity!="critical"`` matches an alert with no ``severity`` at all.
        Treating absent as "no match" instead would quietly drop routes.
        """
        actual = str(labels.get(self.name, ""))
        if self.op == "=":
            return actual == self.value
        if self.op == "!=":
            return actual != self.value
        if self._rx is None:
            # An unparseable regex must not silently match everything.
            return self.op == "!~"
        hit = bool(self._rx.match(actual))
        return hit if self.op == "=~" else not hit

    def describe(self) -> str:
        return f"{self.name}{self.op}{self.value!r}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"Matcher({self.describe()})"


def parse_matchers(node: dict) -> list[Matcher]:
    """Read every matcher shape Grafana has ever written into a policy.

    All three coexist in the wild — ``object_matchers`` is current, ``match`` and
    ``match_re`` are the deprecated Alertmanager originals that survive in
    policies provisioned years ago and are still honoured by the server. Reading
    only the modern one would treat an old route as "matches everything" and
    report the wrong receiver with total confidence.
    """
    out: list[Matcher] = []
    for raw in node.get("object_matchers") or []:
        if isinstance(raw, (list, tuple)) and len(raw) == 3:
            try:
                out.append(Matcher(str(raw[0]), str(raw[1]), str(raw[2])))
            except ValueError:
                continue
    for name, value in (node.get("match") or {}).items():
        out.append(Matcher(str(name), "=", str(value)))
    for name, value in (node.get("match_re") or {}).items():
        out.append(Matcher(str(name), "=~", str(value)))
    # `matchers` is the string form: ["severity=critical", 'team=~"be.*"'].
    for raw in node.get("matchers") or []:
        m = _parse_matcher_string(str(raw))
        if m:
            out.append(m)
    return out


def _parse_matcher_string(text: str) -> Matcher | None:
    for op in _OPS:
        idx = text.find(op)
        if idx > 0:
            name = text[:idx].strip()
            value = text[idx + len(op):].strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            try:
                return Matcher(name, op, value)
            except ValueError:
                return None
    return None


def resolve_receivers(tree: dict, labels: dict[str, str]) -> list[dict]:
    """Walk the policy tree; return the matched leaf routes, in order.

    A faithful port of Alertmanager's ``Route.Match``:

    * a node only participates if **its own** matchers pass (the root has none, so
      it always does);
    * children are tried **in order**; the first match wins and the walk stops —
      unless that child sets ``continue: true``, which keeps the siblings in play
      and is how one alert reaches two receivers;
    * if **no child matched**, the node itself is the match.

    Each returned entry carries the ``path`` that got there, because "which route
    caught this" is the actual debugging question and a bare receiver name does
    not answer it.
    """
    return _match_node(tree, labels, path=["<root>"], inherited=tree.get("receiver"))


def _match_node(node: dict, labels: dict[str, str], *, path: list[str], inherited: str | None) -> list[dict]:
    if not all(m.matches(labels) for m in parse_matchers(node)):
        return []

    # An empty receiver on a child means "keep the parent's" -- Alertmanager
    # inherits down the tree rather than defaulting to nothing.
    receiver = node.get("receiver") or inherited

    matched: list[dict] = []
    for child in node.get("routes") or []:
        if not isinstance(child, dict):
            continue
        child_path = path + [_label_of(child)]
        hits = _match_node(child, labels, path=child_path, inherited=receiver)
        matched.extend(hits)
        if hits and not child.get("continue"):
            break

    if matched:
        return matched
    return [
        {
            "receiver": receiver,
            "path": path,
            "muteTimeIntervals": list(node.get("mute_time_intervals") or []),
            "groupBy": list(node.get("group_by") or []),
        }
    ]


def _label_of(node: dict) -> str:
    """A human-readable name for a route in a path, since routes have no id."""
    matchers = parse_matchers(node)
    if matchers:
        return " & ".join(m.describe() for m in matchers)
    return node.get("receiver") or "<route>"


def integration_names(receiver: dict) -> list[str]:
    """The delivery mechanisms on a receiver.

    Two shapes, and the difference matters: the **provisioning** API calls them
    ``grafana_managed_receiver_configs``, the **alertmanager** API calls them
    ``integrations``. Read one and you see an empty receiver that is actually
    fine, or a full one that is actually empty.
    """
    raw = receiver.get("integrations")
    if raw is None:
        raw = receiver.get("grafana_managed_receiver_configs") or []
    names: list[str] = []
    for item in raw or []:
        if isinstance(item, dict):
            names.append(str(item.get("type") or item.get("name") or "unknown"))
        else:
            names.append(str(item))
    return names


def delivery_report(
    labels: dict[str, str],
    tree: dict,
    receivers: list[dict],
    *,
    rule: str | None = None,
) -> dict:
    """Join rule labels -> policy tree -> receivers -> integrations.

    The ``delivered`` boolean is the whole point: it is false when an alert routes
    to a receiver that exists but cannot deliver, which is invisible in every
    other view Grafana offers.
    """
    by_name = {str(r.get("name")): r for r in receivers or [] if isinstance(r, dict)}
    matches = resolve_receivers(tree or {}, labels or {})

    routes: list[dict] = []
    for m in matches:
        name = m.get("receiver")
        receiver = by_name.get(str(name))
        integrations = integration_names(receiver) if receiver else []
        if receiver is None:
            problem = (
                f"the policy routes to a receiver named {name!r}, but no such "
                f"contact point exists. Nothing will be delivered."
            )
        elif not integrations:
            problem = (
                f"contact point {name!r} exists but has no integrations "
                f"configured — no email, no webhook, nothing. Alerts matching this "
                f"route fire into the void, and every screen in Grafana will still "
                f"show them as firing normally."
            )
        else:
            problem = None
        routes.append(
            {
                "receiver": name,
                "exists": receiver is not None,
                "integrations": integrations,
                "path": m.get("path"),
                "muteTimeIntervals": m.get("muteTimeIntervals") or [],
                "groupBy": m.get("groupBy") or [],
                "problem": problem,
            }
        )

    delivered = any(r["integrations"] and not r["muteTimeIntervals"] for r in routes)
    report = {
        "labels": labels,
        "routes": routes,
        "delivered": delivered,
        "problems": [r["problem"] for r in routes if r["problem"]],
    }
    if rule:
        report["rule"] = rule
    if not delivered and not report["problems"]:
        # Every route had integrations but all were muted -- worth its own words,
        # because "muted" looks like "working" in the API response.
        report["problems"] = [
            "every matching route is covered by a mute timing, so nothing will be "
            "delivered while that timing is active."
        ]
    return report


def rule_labels(rule: dict) -> dict[str, str]:
    """The labels an alert instance from *rule* will carry into routing.

    Grafana injects ``alertname`` and ``grafana_folder`` server-side rather than
    storing them on the rule, and the default policy groups by exactly those two.
    Routing a rule's own ``labels`` dict alone therefore models the wrong alert:
    any route matching on ``alertname`` would be missed.

    **Pass ``folderTitle`` if you possibly can.** Verified live: the injected
    ``grafana_folder`` value is the folder's *title*, not its uid — yet
    ``GET /api/v1/provisioning/alert-rules`` returns only ``folderUID``. So the
    uid fallback below is a **last resort that can be wrong**: a policy branching
    on ``grafana_folder="Some Folder"`` will not match ``c7k2m9p4q1r8sd``, and the
    report would name the root receiver with total confidence. Callers resolve the
    title first (``GET /api/folders/{uid}``); this function stays pure and takes
    whatever it is given.
    """
    labels = dict(rule.get("labels") or {})
    labels.setdefault("alertname", str(rule.get("title") or ""))
    folder = rule.get("folderTitle") or rule.get("folderUID")
    if folder:
        labels.setdefault("grafana_folder", str(folder))
    return labels
