"""Log analysis: fingerprinting, clustering and classification.

Pure logic, no HTTP. This is the engine behind `grafana-cli scan` ("does this project
work? anything irregular?") and `grafana-cli logs similar` ("has this happened
elsewhere?").

**The idea.** Ten thousand log lines are not ten thousand problems — they are
usually a dozen problems repeated. So the unit of analysis here is not the line,
it is the **fingerprint**: the line with its variable parts (ids, timestamps,
durations, addresses) replaced by placeholders, so that

    connection to 10.0.0.7:5432 failed after 1.2s
    connection to 10.0.0.9:5432 failed after 0.4s

collapse to one finding seen twice, rather than two findings seen once. That
collapse is what makes "anything irregular?" answerable in a context window, and
it is also, run against one line instead of a corpus, exactly the query you need
for "show me this happening somewhere else".

**On the classifier.** The patterns below are heuristics over log *prose*, and
prose lies: a line saying `no errors found` contains "error". So a category is a
**hint that ranks output**, never a verdict, and the raw line always travels with
it so the reader can overrule. The one signal that is not a heuristic is Loki's
own `detected_level`, which is why it is preferred over the text whenever present.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

#: Ordered: each pattern runs against the output of the last, so the most
#: specific must come first. A UUID must be eaten before the bare-number rule
#: gets to it, or the fingerprint keeps the dashes and every id looks distinct.
_NORMALISERS: tuple[tuple[re.Pattern, str], ...] = (
    # ISO-8601 / RFC-3339 timestamps, with or without zone.
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    # Syslog's "Jul 17 08:53:26".
    (re.compile(r"\b[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b"), "<TS>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    # IPv4, optional port.
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ADDR>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<HEX>"),
    # Long hex runs: git shas, container ids, request ids. 7 is git's short-sha
    # floor -- shorter and we would start eating English words like "decade".
    (re.compile(r"\b[0-9a-f]{7,}\b", re.I), "<HEX>"),
    # Opaque alphanumeric ids that are NOT hex: Docker Swarm node/service/task
    # ids, Kubernetes uids, ULIDs, nanoids, base62 tokens. Found the hard way --
    # a live `scan` reported 204 "distinct problems" from 256 lines that were all
    # one problem, because every line carried `task.id=54syhoorqy...` and the hex
    # rule cannot match an id containing s, x, p or z. Without this, fingerprinting
    # silently stops collapsing anything emitted by modern orchestrators, which is
    # to say: most things.
    #
    # Two guards keep it off real words. Length >= 16 puts it well clear of
    # English, and the lookaheads demand BOTH a letter and a digit -- so
    # "taskmanager" and "authentication" are untouched while "sxbx71pvrl5cbzizfk"
    # is not. It runs after the hex rules so a git sha still reads as <HEX>.
    (re.compile(r"\b(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{16,}\b"), "<ID>"),
    # Durations before bare numbers, so "1.2s" is one token and not "<N>s".
    (re.compile(r"\b\d+(?:\.\d+)?(?:ns|us|ms|s|m|h)\b"), "<DUR>"),
    # No trailing \b here, deliberately: `%` is not a word character, so `95%\b`
    # can never match at end-of-string. A trailing boundary silently disabled the
    # whole percent branch -- caught only because a test asserted `at 95%`.
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:[KMGT]i?B\b|%)", re.I), "<SIZE>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<N>"),
)

_WS_RE = re.compile(r"\s+")

#: Category -> (severity, patterns, what it means).
#: Severity orders the report; the text is what a reader acts on.
_CATEGORIES: tuple[tuple[str, int, tuple[str, ...], str], ...] = (
    ("panic", 90, (r"\bpanic\b", r"\bgoroutine\s+\d+\b", r"\bSIGSEGV\b", r"\bsegmentation fault\b",
                   r"\bnil pointer dereference\b", r"\bunhandled exception\b", r"\bTraceback \(most recent call last\)"),
     "a process crashed or hit an unrecoverable bug"),
    ("oom", 85, (r"\bout of memory\b", r"\bOOMKilled\b", r"\boom[-_ ]?kill", r"\bcannot allocate memory\b",
                 r"\bMemoryError\b"),
     "something was killed for using too much memory"),
    ("disk", 80, (r"\bno space left on device\b", r"\bdisk (?:is )?full\b", r"\bquota exceeded\b"),
     "a filesystem is full"),
    ("fatal", 75, (r"\bfatal\b", r"\bemergency\b"), "a process gave up"),
    ("cert", 60, (r"\bx509\b", r"\bcertificate (?:has expired|is not valid|verify failed)",
                  r"\btls: (?:handshake|bad certificate)", r"\bSSL certificate problem\b"),
     "a TLS/certificate problem"),
    ("auth", 55, (r"\bpermission denied\b", r"\baccess denied\b", r"\bunauthorized\b", r"\bforbidden\b",
                  r"\bauthentication failed\b", r"\binvalid credentials\b"),
     "something was refused permission"),
    ("connection", 50, (r"\bconnection refused\b", r"\bconnection reset\b", r"\bno such host\b",
                        r"\bi/o timeout\b", r"\bcontext deadline exceeded\b", r"\bdial tcp\b",
                        r"\bunreachable\b", r"\bEOF\b", r"\btimeout\b", r"\btimed out\b"),
     "something could not be reached"),
    ("deprecation", 30, (r"\bdeprecat", r"\bwill be removed in\b", r"\bno longer supported\b",
                         r"\bobsolete\b", r"\blegacy\b.{0,20}\bremoved\b"),
     "something still works but is on borrowed time"),
    # NOTE the `s?` on every noun. `\berror\b` does not match "errors" -- the \b
    # needs a non-word char and finds the plural's `s` -- so "5 errors occurred"
    # went unclassified. Under-claiming is the worse failure direction for a tool
    # whose job is finding problems, and only a test caught it.
    ("error", 20, (r"\berrors?\b", r"\bfailed\b", r"\bfailures?\b", r"\bexceptions?\b", r"\bcannot\b",
                   r"\bcould not\b", r"\brefused\b", r"\brejected\b"),
     "a generic error"),
)

_COMPILED: tuple[tuple[str, int, tuple[re.Pattern, ...], str], ...] = tuple(
    (name, sev, tuple(re.compile(p, re.I) for p in pats), blurb)
    for name, sev, pats, blurb in _CATEGORIES
)

#: `detected_level` values that mean "this is a problem". Loki derives these
#: itself, so they beat any amount of prose-sniffing.
_BAD_LEVELS = {"error", "fatal", "critical", "crit", "emerg", "alert"}

CATEGORY_HELP = {name: blurb for name, _sev, _pats, blurb in _CATEGORIES}


def fingerprint(line: str) -> str:
    """Reduce a log line to its shape, so repeats of one problem collapse.

    ``connection to 10.0.0.7:5432 failed after 1.2s`` ->
    ``connection to <ADDR> failed after <DUR>``
    """
    text = str(line or "")
    for pattern, placeholder in _NORMALISERS:
        text = pattern.sub(placeholder, text)
    return _WS_RE.sub(" ", text).strip()


def classify(line: str, labels: dict | None = None) -> tuple[str | None, int]:
    """Best-guess ``(category, severity)`` for one line.

    Loki's own ``detected_level`` is consulted first and is allowed to *raise* the
    floor, never to veto: a line marked ``info`` that says "panic: nil pointer" is
    a panic whatever the level field claims, because levels are assigned by the
    emitting library and panics are frequently logged at the wrong one.
    """
    text = str(line or "")
    level = str((labels or {}).get("detected_level") or "").lower()

    for name, sev, patterns, _blurb in _COMPILED:
        if any(p.search(text) for p in patterns):
            return name, sev

    if level in _BAD_LEVELS:
        # Loki says this is bad and the prose gave us nothing more specific.
        return "error", 20
    return None, 0


def cluster(records: Iterable[dict], *, top: int = 10, examples: int = 1) -> list[dict]:
    """Group records by fingerprint; return the biggest clusters first.

    Each cluster carries ``sources`` — the distinct label sets it was seen on —
    because "this error is on one host" and "this error is on all twenty-one" are
    completely different problems that look identical in a flat log tail.
    """
    buckets: dict[str, dict] = {}
    for rec in records:
        line = rec.get("line", "")
        fp = fingerprint(line)
        if not fp:
            continue
        category, severity = classify(line, rec.get("labels"))
        bucket = buckets.get(fp)
        if bucket is None:
            bucket = buckets[fp] = {
                "fingerprint": fp,
                "count": 0,
                "category": category,
                "severity": severity,
                "examples": [],
                "_sources": Counter(),
                "firstSeen": rec.get("time"),
                "lastSeen": rec.get("time"),
                "_first_ts": rec.get("timestamp"),
                "_last_ts": rec.get("timestamp"),
            }
        bucket["count"] += 1
        if len(bucket["examples"]) < examples:
            bucket["examples"].append(line)
        source = _source_of(rec.get("labels") or {})
        if source:
            bucket["_sources"][source] += 1
        _track_window(bucket, rec)

    out = []
    for bucket in buckets.values():
        sources = bucket.pop("_sources")
        bucket.pop("_first_ts", None)
        bucket.pop("_last_ts", None)
        bucket["sources"] = [{"source": s, "count": c} for s, c in sources.most_common(5)]
        bucket["sourceCount"] = len(sources)
        out.append(bucket)

    # Severity first, then volume: a single panic outranks a thousand timeouts,
    # because the panic is the thing you have to go and fix.
    out.sort(key=lambda b: (-(b["severity"] or 0), -b["count"]))
    return out[:top] if top else out


def _track_window(bucket: dict, rec: dict) -> None:
    ts = rec.get("timestamp")
    if ts is None:
        return
    if bucket["_first_ts"] is None or ts < bucket["_first_ts"]:
        bucket["_first_ts"], bucket["firstSeen"] = ts, rec.get("time")
    if bucket["_last_ts"] is None or ts > bucket["_last_ts"]:
        bucket["_last_ts"], bucket["lastSeen"] = ts, rec.get("time")


def _source_of(labels: dict) -> str | None:
    """The most human-meaningful identity available for a stream.

    Ordered by how specific each label is in practice on a real instance: the
    unit/container names the *thing*, the host only names where it ran.
    """
    for key in ("systemd_unit", "container", "container_name", "app", "service_name", "job", "pod", "hostname", "instance"):
        value = labels.get(key)
        if value:
            return f"{key}={value}"
    return None


def to_regex(fp: str) -> str:
    """Turn a fingerprint back into a LogQL regex that finds its siblings.

    This is the "has this happened elsewhere?" move: take one line, reduce it to
    its shape, then search for that shape across every source. The placeholders
    become permissive patterns and the literal text is escaped, so the result
    matches the *same* message with *different* ids.

    Anchoring is deliberately absent: log lines arrive with prefixes (a syslog
    header, a container tag) that vary by collector, and anchoring would match
    none of them.
    """
    placeholders = {
        "<TS>": r"\S+",
        "<UUID>": r"[0-9a-fA-F-]+",
        "<ADDR>": r"[0-9.:]+",
        "<HEX>": r"[0-9a-fA-Fx]+",
        "<ID>": r"[A-Za-z0-9]+",
        "<DUR>": r"[0-9.]+\w*",
        "<SIZE>": r"[0-9.]+\s?\w*",
        "<N>": r"[0-9.]+",
    }
    # Split on the placeholders, escape everything between them, then rejoin.
    parts = re.split(r"(<TS>|<UUID>|<ADDR>|<HEX>|<ID>|<DUR>|<SIZE>|<N>)", fp)
    out = []
    for part in parts:
        if part in placeholders:
            out.append(placeholders[part])
        elif part:
            out.append(re.escape(part))
    return "".join(out)


def summarise(records: list[dict], clusters: list[dict]) -> dict:
    """The headline for `scan`: the numbers a reader needs before the detail."""
    by_category: Counter = Counter()
    for c in clusters:
        if c.get("category"):
            by_category[c["category"]] += c["count"]
    worst = clusters[0] if clusters else None
    return {
        "linesAnalysed": len(records),
        "distinctProblems": len(clusters),
        "byCategory": dict(by_category.most_common()),
        "worst": (
            {
                "category": worst.get("category"),
                "count": worst.get("count"),
                "fingerprint": worst.get("fingerprint"),
            }
            if worst
            else None
        ),
    }


def verdict(clusters: list[dict]) -> dict:
    """Turn the findings into the one-line answer `scan` was asked for.

    ``healthy`` is deliberately conservative — it is false whenever anything at
    all was classified, because a tool that says "looks fine" while a panic sits
    in the output has actively misled someone. "I found things, here they are,
    you judge" is the honest contract for a heuristic.
    """
    if not clusters:
        return {
            "healthy": True,
            "summary": "no errors, panics or deprecations matched in this window.",
        }
    top = clusters[0]
    severity = top.get("severity") or 0
    counts = ", ".join(
        f"{c['count']}x {c.get('category') or 'unclassified'}" for c in clusters[:3]
    )
    return {
        "healthy": False,
        "summary": (
            f"{len(clusters)} distinct problem(s); worst is "
            f"{top.get('category') or 'unclassified'} "
            f"({CATEGORY_HELP.get(top.get('category') or '', 'see the line')}). Top: {counts}."
        ),
        "severity": severity,
    }


def looks_like_json(line: str) -> bool:
    """Whether a log line is itself JSON — worth knowing before parsing prose.

    Used to tell the caller "these logs are structured, `| json` will work on
    them", which changes what queries are worth suggesting.
    """
    s = str(line or "").strip()
    return len(s) > 1 and s[0] == "{" and s[-1] == "}"


def detect_format(records: list[dict], sample: int = 20) -> str:
    """Guess the payload format of a stream: json | logfmt | text.

    Cheap, and it changes the advice: a `json` stream supports `| json | field=x`,
    a `logfmt` one supports `| logfmt`, and text only supports substring/regex.
    """
    checked = [str(r.get("line") or "") for r in records[:sample] if r.get("line")]
    if not checked:
        return "unknown"
    if sum(1 for line in checked if looks_like_json(line)) > len(checked) / 2:
        return "json"
    # logfmt: at least two key=value pairs, which is what distinguishes it from
    # prose that merely contains an equals sign.
    logfmt = re.compile(r"(?:^|\s)[A-Za-z_][\w.-]*=(?:\"[^\"]*\"|\S+)")
    if sum(1 for line in checked if len(logfmt.findall(line)) >= 2) > len(checked) / 2:
        return "logfmt"
    return "text"


def as_records(payload: Any) -> list[dict]:
    """Accept either raw Loki JSON or already-parsed records.

    Convenience for the command layer and the tests, which reach this module from
    both directions.
    """
    from .loki import parse_streams

    if isinstance(payload, dict):
        return parse_streams(payload)
    return list(payload or [])
