"""LogQL construction and Loki response parsing — pure, and the testable heart.

No HTTP here. Everything below is a function from data to data, which is what
makes the query builder testable without a Loki (see tests/test_loki_unit.py).

Three facts drive the design, all verified live against the instance in
``spike/VERIFIED_FINDINGS.md``:

1. **A stream selector cannot be empty.** ``{}`` is a parse error in Loki, so
   "give me everything" has to be spelled with a real matcher. We use
   ``{<some-label>=~".+"}`` and pick the label deliberately — see `match_all`.
2. **`detected_level` is not a stream label.** It does not appear in ``/labels``,
   yet it comes back on every stream and *does* filter — because Loki derives it
   at query time. That means it is legal in a **pipeline stage**
   (``{job=~".+"} | detected_level="error"``) and illegal in the **selector**
   (``{detected_level="error"}`` matches nothing). The distinction is invisible in
   the docs and is the whole reason `level=` is threaded separately below.
3. **Label cardinality is the useful signal, not label names.** Two of the four
   labels on the live instance have exactly one value, so "you can filter on
   `job`" is worthless advice. Callers get counts.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from agentcli.errors import ValidationError

#: Loki's operators for a stream matcher.
MATCH_OPS = ("=~", "!~", "!=", "=")

#: Levels Loki's own `detected_level` derivation emits. Anything outside this set
#: is still passed through -- the list is for validation hints and `--level`
#: completion, not a gate, because Loki adds to it between versions.
KNOWN_LEVELS = ("trace", "debug", "info", "warn", "error", "fatal", "critical", "unknown")


def escape_value(value: str) -> str:
    """Escape a label value for a LogQL double-quoted string.

    Order matters: backslash first, or you escape your own escapes. A label value
    containing a quote is not hypothetical here — systemd unit names and k8s
    annotations both carry them.
    """
    return str(value).replace("\\", "\\\\").replace("\"", "\\\"")


def build_selector(matchers: dict[str, str] | None = None, *, match_all_label: str | None = None) -> str:
    """Build a stream selector: ``{a="b", c=~"d.*"}``.

    A value may carry its own operator prefix (``~`` for regex, ``!`` for
    negation); otherwise it is an exact match. So ``{"hostname": "~web.*"}`` ->
    ``hostname=~"web.*"``.

    Raises rather than emitting ``{}``: Loki rejects an empty selector with a
    parse error, and a parse error surfaced from three layers down reads like a
    bug in this tool. If you want everything, pass `match_all_label`.
    """
    parts: list[str] = []
    for key, raw in (matchers or {}).items():
        if not _is_label_name(key):
            raise ValidationError(
                f"{key!r} is not a valid label name. Label names are letters, "
                f"digits and underscores, and cannot start with a digit."
            )
        op, value = _split_op(raw)
        parts.append(f'{key}{op}"{escape_value(value)}"')

    if not parts:
        if not match_all_label:
            raise ValidationError(
                "a log query needs at least one label matcher — Loki rejects an "
                "empty '{}' selector. Run `graf logs sources` to see which labels "
                "exist and how many values each has."
            )
        parts.append(f'{match_all_label}=~".+"')
    return "{" + ", ".join(parts) + "}"


def _split_op(raw: str) -> tuple[str, str]:
    """Pull a leading operator hint off a value. ``"~web.*"`` -> ``("=~", "web.*")``."""
    s = str(raw)
    if s.startswith("!~"):
        return "!~", s[2:]
    if s.startswith("!="):
        return "!=", s[2:]
    if s.startswith("!"):
        return "!=", s[1:]
    if s.startswith("=~"):
        return "=~", s[2:]
    if s.startswith("~"):
        return "=~", s[1:]
    return "=", s


def _is_label_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)))


def build_query(
    matchers: dict[str, str] | None = None,
    *,
    contains: Iterable[str] | None = None,
    excludes: Iterable[str] | None = None,
    regex: str | None = None,
    level: str | None = None,
    match_all_label: str | None = None,
) -> str:
    """Assemble a full LogQL query.

    ``level`` lands in a **pipeline stage**, never the selector — see the module
    docstring. That single detail is the difference between a query that finds
    every error on the box and one that silently returns nothing.
    """
    q = build_selector(matchers, match_all_label=match_all_label)
    for needle in contains or []:
        q += f' |= "{escape_value(needle)}"'
    for needle in excludes or []:
        q += f' != "{escape_value(needle)}"'
    if regex:
        _validate_regex(regex)
        q += f' |~ "{escape_value(regex)}"'
    if level:
        # NOT `{detected_level="error"}` -- that matches nothing, because Loki
        # derives the label at query time and it is absent from the index.
        q += f' | detected_level="{escape_value(level)}"'
    return q


def _validate_regex(pattern: str) -> None:
    """Fail here, not 400 rungs later.

    Loki uses RE2 and Python uses PCRE, so this is a *sniff test*, not a proof:
    it catches the unbalanced-bracket typos that are 90% of real mistakes, while
    a few valid-in-one-invalid-in-the-other patterns will still make it through
    to Loki. Better a good local error for the common case than none at all.
    """
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValidationError(f"invalid regex {pattern!r}: {exc}") from exc


# ---- response parsing ------------------------------------------------


def parse_streams(payload: Any) -> list[dict]:
    """Flatten Loki's ``streams`` response into a list of log records.

    Loki hands back one entry per *stream*, each with its own label set and a
    list of ``[nanos, line]`` pairs — a shape that is convenient for Loki and
    useless for anyone reading. We flatten to one record per line, carrying the
    labels down, and sort newest-first so `--limit` means "the most recent N".

    Returns ``[{"timestamp": ns, "time": iso, "line": str, "labels": {...}}]``.
    """
    result = _result_of(payload)
    out: list[dict] = []
    for stream in result:
        if not isinstance(stream, dict):
            continue
        labels = stream.get("stream") or {}
        for pair in stream.get("values") or []:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            ts_raw, line = pair[0], pair[1]
            try:
                ts = int(ts_raw)
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "timestamp": ts,
                    "time": _iso_from_nanos(ts),
                    "line": line,
                    "labels": dict(labels),
                }
            )
    out.sort(key=lambda r: r["timestamp"], reverse=True)
    return out


def _result_of(payload: Any) -> list:
    """Dig the result array out, tolerating the shapes Loki actually returns."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    result = data.get("result")
    return result if isinstance(result, list) else []


def _iso_from_nanos(ns: int) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_label_values(payload: Any) -> list[str]:
    """``{"status":"success","data":["a","b"]}`` -> ``["a","b"]``.

    A missing/absent ``data`` is an empty list, not an error: Loki returns exactly
    that for a label with no values in the window, which is normal, not broken.
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    return [str(v) for v in data] if isinstance(data, list) else []


def summarise_labels(labels: dict[str, list[str]], *, sample: int = 5) -> list[dict]:
    """Turn ``{label: [values]}`` into the cardinality report `logs sources` emits.

    ``useful`` is the opinion this function exists to express. A label with one
    value cannot narrow anything down — on the live instance two of the four are
    exactly that — so listing it as a filter you "can use" is noise dressed up as
    help. Sorting by cardinality puts the labels that actually discriminate first.
    """
    out = [
        {
            "label": name,
            "values": len(values),
            "useful": len(values) > 1,
            "sample": sorted(values)[:sample],
        }
        for name, values in labels.items()
    ]
    out.sort(key=lambda r: (-r["values"], r["label"]))
    return out


def pick_match_all_label(labels: dict[str, list[str]]) -> str | None:
    """Choose a label for a ``{x=~".+"}`` match-everything selector.

    Prefers the **highest-cardinality** label, and that is not arbitrary: a
    match-all selector only returns streams that *carry* the label, so choosing a
    rare one silently drops every stream missing it. The label present on the most
    distinct values is the best available proxy for "present everywhere".
    """
    if not labels:
        return None
    return max(labels.items(), key=lambda kv: (len(kv[1]), kv[0]))[0]
