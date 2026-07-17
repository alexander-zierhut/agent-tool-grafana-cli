"""Datasource discovery — the feature Grafana does not have.

Thin: it takes a `Client` and does the enumerate-and-classify walk. The pure
parts live in :mod:`loki` and are tested without a server.

**Why this module is the product.** Grafana can tell you a datasource's config if
you already know its uid, and it can run a query if you already know the label to
filter on. What it will not tell you, anywhere, in the UI or the API, is *"what
can I get logs from?"* — you are expected to already know. Answering that means:

    GET /api/datasources                     -> everything, of every type
      filter to the types that carry logs    -> loki, elasticsearch, …
        for each: GET .../labels             -> what you can filter on
          for each label: .../values         -> and how much it discriminates

Four calls deep, and the last step is the one that matters: on the live instance
two of Loki's four labels have exactly **one** value, so half the "filters" are
decorative. A tool that lists label names without counting values is telling you
the same non-answer Grafana does, just faster.
"""

from __future__ import annotations

from typing import Any

from agentcli.errors import ConfigError, NotFoundError

from .config import LOG_DATASOURCE_TYPES, METRIC_DATASOURCE_TYPES
from .errors import AuthError, DatasourceUnreachable, OpError
from .loki import parse_label_values, summarise_labels
from .timerange import TimeRange


def list_datasources(client) -> list[dict]:
    """Every datasource in this org.

    Needs ``datasources:read`` (org Admin by default). The client turns the 403
    into a message that names the permission and says what it costs you, because
    "forbidden" here means "discovery is impossible" and the reader deserves to
    know which of the two they hit.
    """
    data = client.get("/datasources")
    return data if isinstance(data, list) else []


def classify(ds: dict) -> dict:
    """Annotate a datasource with what this CLI can do with it.

    ``recognised`` is a deliberate third state between supported and absent: a
    datasource this tool cannot query still *exists*, and hiding it would answer
    "what can I get logs from" with a lie. Better to name it and say so.
    """
    kind = str(ds.get("type") or "")
    logs = LOG_DATASOURCE_TYPES.get(kind)
    metrics = METRIC_DATASOURCE_TYPES.get(kind)
    return {
        "uid": ds.get("uid"),
        "name": ds.get("name"),
        "type": kind,
        "isDefault": bool(ds.get("isDefault")),
        "logs": logs or None,
        "metrics": metrics or None,
        "queryable": logs == "supported" or metrics == "supported",
    }


def log_datasources(client) -> list[dict]:
    """Datasources that carry logs, annotated. Ordered: usable ones first."""
    out = [classify(d) for d in list_datasources(client)]
    out = [d for d in out if d["logs"]]
    out.sort(key=lambda d: (d["logs"] != "supported", not d["isDefault"], str(d["name"])))
    return out


def metric_datasources(client) -> list[dict]:
    out = [classify(d) for d in list_datasources(client)]
    out = [d for d in out if d["metrics"]]
    out.sort(key=lambda d: (d["metrics"] != "supported", not d["isDefault"], str(d["name"])))
    return out


def resolve(client, ref: str | None, *, kind: str = "logs") -> dict:
    """Turn ``--datasource`` into a concrete datasource, or explain.

    Accepts a uid or a name, because a human reads names off the UI and an agent
    copies uids out of JSON, and refusing either is a papercut. Name matching is
    case-insensitive; uid matching is not (Grafana's uids are case-sensitive).

    With no ref at all, auto-picks **only if there is exactly one** candidate.
    Guessing among several would be a coin flip whose result looks authoritative
    — and on this instance the wrong pick is not an error, it is a confident
    empty result from the wrong tenant.
    """
    pool = log_datasources(client) if kind == "logs" else metric_datasources(client)
    supported = [d for d in pool if (d.get(kind) == "supported")]

    if ref:
        for d in pool:
            if d["uid"] == ref:
                return d
        matches = [d for d in pool if str(d["name"]).lower() == str(ref).lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            uids = ", ".join(str(d["uid"]) for d in matches)
            raise ConfigError(
                f"{ref!r} matches {len(matches)} datasources in this org ({uids}). "
                f"Pass the uid instead of the name."
            )
        known = ", ".join(f"{d['name']} ({d['uid']})" for d in pool) or "none"
        raise NotFoundError(
            f"no {kind} datasource {ref!r} in this org. Available: {known}. "
            f"Note that datasource uids are per-org — if you copied this one from "
            f"another profile, it will not exist here."
        )

    if not supported:
        if pool:
            names = ", ".join(f"{d['name']} ({d['type']})" for d in pool)
            raise ConfigError(
                f"this org has {kind} datasources ({names}), but none of a type this "
                f"CLI can query yet. Only "
                f"{'Loki' if kind == 'logs' else 'Prometheus/Mimir'} is implemented."
            )
        raise ConfigError(
            f"no {kind} datasource in this org. `graf datasource list` shows what exists."
        )
    if len(supported) == 1:
        return supported[0]
    names = ", ".join(f"{d['name']} ({d['uid']})" for d in supported)
    raise ConfigError(
        f"this org has {len(supported)} {kind} datasources: {names}. Pick one with "
        f"--datasource, or make it stick: `graf context set --datasource <uid>`."
    )


# ---- Loki label discovery -------------------------------------------


def loki_labels(client, uid: str, window: TimeRange) -> list[str]:
    """The label names Loki will accept in a selector, for this window.

    **Time-bounded, and that is not a detail.** Loki answers from the index over
    the given range, so a label whose streams went quiet drops out of the answer.
    "What can I get logs from?" therefore has a different answer at 09:00 than at
    17:00, and any caller that does not report its window is telling half the
    truth — which is why `window` is required rather than optional here.
    """
    start, end = window.loki()
    payload = client.ds_proxy(uid, "loki/api/v1/labels", params={"start": start, "end": end})
    return parse_label_values(payload)


def loki_label_values(client, uid: str, label: str, window: TimeRange) -> list[str]:
    start, end = window.loki()
    payload = client.ds_proxy(
        uid, f"loki/api/v1/label/{label}/values", params={"start": start, "end": end}
    )
    return parse_label_values(payload)


def describe_loki(client, uid: str, window: TimeRange, *, sample: int = 5) -> dict:
    """The full "what can I get logs from here" report for one Loki datasource."""
    labels = loki_labels(client, uid, window)
    values = {name: loki_label_values(client, uid, name, window) for name in labels}
    return {
        "labels": summarise_labels(values, sample=sample),
        "labelCount": len(labels),
        "window": window.describe(),
        # Loki derives some labels at query time -- `detected_level` is the one
        # that matters -- so they are absent here yet usable in a query. Saying
        # so is the difference between a complete answer and a confident partial
        # one; see loki.build_query.
        "note": (
            "these are the INDEXED labels for this window. Loki also derives labels "
            "at query time that do not appear here — notably `detected_level`, which "
            "you can filter on with `graf logs query --level error`. Labels with one "
            "value cannot narrow anything down."
        ),
    }


def survey(client, window: TimeRange, *, sample: int = 5) -> dict:
    """Every log datasource in this org, with its labels and their cardinality.

    Errors are **captured per datasource, never raised**, for the same reason
    `server doctor` never raises: the report is the deliverable. One dead backend
    (the live instance has exactly that — a proxy returning 502) must not blank
    out the datasources that are fine. So a broken one is reported as broken,
    beside the working ones.
    """
    out: list[dict] = []
    for ds in log_datasources(client):
        entry = dict(ds)
        if ds["logs"] != "supported":
            entry["error"] = f"type {ds['type']!r} is recognised but not implemented yet"
            out.append(entry)
            continue
        try:
            entry.update(describe_loki(client, str(ds["uid"]), window, sample=sample))
        except DatasourceUnreachable as exc:
            entry["error"] = str(exc)
            entry["reachable"] = False
        except AuthError as exc:
            entry["error"] = str(exc)
        except OpError as exc:
            # Ends in OpError on purpose. NotFoundError and ApiError are SIBLINGS
            # in this taxonomy, so an `except ApiError` ladder looks exhaustive
            # and silently is not -- that exact trap leaked a NotFoundError out
            # of drone's `server doctor`.
            entry["error"] = str(exc)
        else:
            entry["reachable"] = True
        out.append(entry)
    return {"window": window.describe(), "datasources": out}


def health(client, uid: str) -> dict:
    """Ask Grafana to test a datasource.

    Not universally available (it is a per-plugin resource endpoint), so a failure
    here is reported, not raised — "I could not check" and "it is broken" are
    different answers and must not be flattened into one.
    """
    try:
        return {"uid": uid, "ok": True, "detail": client.get(f"/datasources/uid/{uid}/health")}
    except OpError as exc:
        return {"uid": uid, "ok": False, "detail": str(exc)}


def context_datasource(obj: Any, kind: str = "logs") -> str | None:
    """The sticky datasource for the active profile, if any.

    Per-profile by construction (see config.Config.context): a datasource uid from
    org 1 is meaningless in org 6, so a global sticky default would hand Grafana a
    uid that does not exist and get back a 404 blaming the datasource.
    """
    key = "datasource" if kind == "logs" else "metrics_datasource"
    return (obj.config.context or {}).get(key)
