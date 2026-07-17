"""`grafana-cli scan` — the headline command: "does this project work? anything irregular?"

An agent runs this once, right after a deploy, with no more context than a
datasource and maybe a label narrowing it to "the project". It has to come back
with one payload that says, honestly, whether anything is wrong and where to
look next — not a raw log dump the agent then has to re-triage itself.

**Why two Loki passes, not one.** `detected_level="error"` (a pipeline stage —
see `loki.build_query`) catches most of it cheaply, but `detected_level` is
assigned by the *emitting library*, not by content: a Go service that panics is
very often still logging at `info` right up to the line that kills it, and a
Python traceback frequently has no level at all. So a second, narrower pass
greps for the handful of severities where that gap matters most — see
`_SEVERITY_REGEX` below. Anything either pass returns is then handed to
`analysis.classify`, which is the actual authority on category/severity; the
regex pass only widens what reaches that classifier, it never assigns a verdict
itself.

**Why bounded, and why in two pieces.** The live instance carries roughly 87
systemd units across 21 hosts (`spike/VERIFIED_FINDINGS.md`) — an unbounded
"give me everything" over even a short window is a context-window accident, and
running it twice (once per pass) would make that worse, not better. `--limit`
caps the TOTAL after the two passes are merged and de-duplicated, not each pass
independently, so raising it has the effect an agent expects.

This module builds on `analysis.py` (fingerprinting, clustering, classifying)
and `loki.py` (query construction, response parsing) — both pure and already
tested; nothing here re-derives what they already do.
"""

from __future__ import annotations

import itertools
import shlex
from typing import Any

import typer

from .. import analysis, loki, sources
from ..errors import OpError, ValidationError
from ._shared import ctx_obj, need_datasource, need_window, parse_label_args

# `--exit-code` opts into this. Deliberately far from the 0-9 taxonomy in
# errors.py (see BUILDING.md §9): "the project is broken" is an OBSERVATION this
# command made successfully, not a failure OF this command, so the default exit
# is 0 even when `verdict.healthy` is false. Mirrors drone's `wait --exit-code`
# band (20-29), independently chosen here for the same reason: an agent that
# does `grafana-cli scan --exit-code && ./deploy-confirmed.sh` needs a code that can
# never collide with "your token is bad" (4) or "that datasource doesn't exist"
# (5/8).
EXIT_UNHEALTHY = 20

# Total log lines analysed, across BOTH passes combined, before clustering. 300
# is chosen, not defaulted from `config.default_limit` (100): that setting is
# tuned for a single listing call (e.g. `logs sources` label values), while scan
# runs two passes over what may be 87 units across 21 hosts and then collapses
# repeats anyway via `analysis.cluster` — 300 raw lines comfortably shows the
# shape of a bad deploy (a dozen distinct problems repeated many times over)
# without either pass alone silently starving the other.
DEFAULT_LIMIT = 300

# Categories a `detected_level="error"` pass is most likely to MISS, mirrored
# from (not imported from — analysis._CATEGORIES is private) analysis.py's four
# highest-severity groups: panic (90), oom (85), disk-full (80), fatal (75).
# Those four share a trait the lower ones don't: by the time the line is
# written the process is often already dying, which is exactly when whatever
# assigned `detected_level` is least likely to have gotten it right. The lower
# categories (cert/auth/connection/deprecation/generic error) are common enough
# that the level pass plus `analysis.classify`'s own regex already catch them
# well after the fact, so duplicating them here would only cost budget for no
# new coverage. `(?i)` because RE2 (Loki's regex engine) supports the same
# inline flag Python does, and log casing is not consistent enough to rely on.
_SEVERITY_REGEX = (
    r"(?i)panic|goroutine \d+|SIGSEGV|segmentation fault|nil pointer dereference|"
    r"unhandled exception|Traceback \(most recent call last\)|"
    r"out of memory|OOMKilled|oom[-_ ]?kill|cannot allocate memory|"
    r"no space left on device|disk (?:is )?full|quota exceeded|"
    r"\bfatal\b|\bemergency\b"
)

_CATEGORY_LIST = ", ".join(analysis.CATEGORY_HELP)


def scan(
    ctx: typer.Context,
    datasource: str = typer.Option(
        None, "--datasource", "-d",
        help="Log datasource uid or name. Default: sticky context, or the only log datasource in this org.",
    ),
    label: list[str] = typer.Option(
        None, "--label", "-l",
        help=(
            "Scope to the project: repeatable KEY=VALUE, e.g. --label systemd_unit=myapp.service "
            "(supports ~/!/!~ prefixes, see `logs query --help`). Omit to scan every stream on the "
            "datasource — see --limit."
        ),
    ),
    since: str = typer.Option(
        None, "--since", help="How far back, e.g. 30m, 2h. Default: the profile's default_since."
    ),
    start: str = typer.Option(
        None, "--from", help="Explicit start (RFC 3339 / unix timestamp / 'now'). Overrides --since."
    ),
    end: str = typer.Option(None, "--to", help="Explicit end. Default: now."),
    limit: int = typer.Option(
        DEFAULT_LIMIT, "--limit",
        help=f"Max lines analysed in TOTAL across both passes, after merging (default {DEFAULT_LIMIT}).",
    ),
    top: int = typer.Option(
        10, "--top", help="Distinct problems to report in `findings`, worst first. 0 = no cap."
    ),
    category: str = typer.Option(
        None, "--category",
        help=(
            f"Only list findings in this category (one of: {_CATEGORY_LIST}). Categories are HINTS "
            f"that rank output, never verdicts — see the docstring. `verdict`/`summary` still cover "
            f"everything found; only `findings` is filtered."
        ),
    ),
    exit_code: bool = typer.Option(
        False, "--exit-code",
        help=f"Exit {EXIT_UNHEALTHY} when unhealthy, instead of the default 0. See the docstring.",
    ),
) -> None:
    """Is this project healthy, and what should I check first?

    The one-shot "I just deployed, did anything break?" check. Resolves a log
    datasource and a time window, runs two targeted Loki queries over it (see
    the module docstring for why two), clusters what comes back by fingerprint
    so repeats collapse into distinct problems, and ranks those by severity —
    one panic outranks a thousand timeouts, because the panic is the one you
    actually have to go fix.

        grafana-cli scan                                          # everything on the default datasource, last hour
        grafana-cli scan --label systemd_unit=myapp.service        # just this project
        grafana-cli scan --since 15m --exit-code && echo "clean"   # gate a deploy script

    **The classifier is a heuristic over log PROSE, not a verdict.** A line that
    says "no errors found" contains the word "error" and WILL be classified as
    one. `category` on each finding ranks output; it is never proof. That is why
    the raw example line travels with every finding — read it before trusting
    the label. `verdict.healthy` is deliberately conservative: it goes false the
    moment anything at all was classified, on the theory that a confident "looks
    fine" sitting next to an unnoticed panic is a worse failure than a false
    alarm over a stray word.

    **Logs only, and that is a real blind spot.** This never looks at metrics —
    `grafana-cli metrics up` is the complementary check for "is the process even
    running". More importantly: a service that logs NOTHING AT ALL in the
    window looks IDENTICAL here to a healthy, quiet one. Silence is not
    evidence of health, only absence of the one signal this command reads.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    window = need_window(obj, since=since, start=start, end=end)
    ds = need_datasource(obj, datasource, kind="logs")
    matchers = parse_label_args(label)

    if category is not None and category not in analysis.CATEGORY_HELP:
        raise ValidationError(f"unknown category {category!r}. One of: {_CATEGORY_LIST}.")

    uid = str(ds["uid"])
    match_all_label = _resolve_match_all(client, uid, window, matchers, ds)

    start_ns, end_ns = window.loki()
    # Half the budget to each pass, floored so a very small --limit still gets a
    # usable sample from both rather than starving one entirely.
    per_pass_limit = max(20, limit // 2) if limit else 0

    error_query = loki.build_query(matchers, level="error", match_all_label=match_all_label)
    regex_query = loki.build_query(matchers, regex=_SEVERITY_REGEX, match_all_label=match_all_label)

    level_records, level_meta = _run_pass(
        client, uid, error_query, start_ns=start_ns, end_ns=end_ns, limit=per_pass_limit, name="level:error"
    )
    regex_records, regex_meta = _run_pass(
        client, uid, regex_query, start_ns=start_ns, end_ns=end_ns, limit=per_pass_limit, name="severity-regex"
    )
    records = _merge(level_records, regex_records, limit=limit)

    log_format = analysis.detect_format(records)
    # Cluster unbounded (top=0) so `verdict`/`summary` describe EVERYTHING this
    # scan found. `--category` only trims the `findings` list below it — a
    # filter must never make an unhealthy scan look healthy just because the
    # worst problem happened to be in a category the caller filtered out.
    all_clusters = analysis.cluster(records, top=0, examples=1)
    summary = analysis.summarise(records, all_clusters)
    verdict = analysis.verdict(all_clusters)

    reported = all_clusters
    if category is not None:
        reported = [c for c in all_clusters if c.get("category") == category]
    findings = reported[:top] if top else reported
    for finding in findings:
        finding["next"] = _next_command(ds, matchers, finding)

    payload: dict[str, Any] = {
        "window": window.describe(),
        "datasource": ds,
        "scope": {"labels": matchers or None, "matchAllLabel": match_all_label},
        "queries": [level_meta, regex_meta],
        "verdict": verdict,
        "summary": summary,
        "logFormat": log_format,
        "categoryFilter": category,
        "findings": findings,
    }
    if category is not None and len(reported) < len(all_clusters):
        hidden = len(all_clusters) - len(reported)
        payload["note"] = (
            f"--category {category!r} is hiding {hidden} other distinct problem(s) found in this "
            f"window — `verdict` and `summary` above still cover ALL of them, only `findings` is "
            f"filtered. Rerun without --category for the full list."
        )

    obj.emitter.emit(payload)

    if exit_code and not verdict["healthy"]:
        raise typer.Exit(code=EXIT_UNHEALTHY)


def _resolve_match_all(client, uid: str, window, matchers: dict[str, str], ds: dict) -> str | None:
    """Pick a match-everything label when no `--label` scoped the query.

    Loki rejects an empty `{}` selector outright (see `loki.build_selector`), so
    "scan the whole datasource" still needs a real matcher. This costs one
    `/labels` call plus one `/label/<name>/values` call per label — metadata,
    not log lines, so it does not touch `--limit` — and reuses
    `loki.pick_match_all_label`'s reasoning: the highest-cardinality label is
    the best available proxy for "present on every stream", because a match-all
    selector silently drops any stream that lacks the chosen label entirely.
    """
    if matchers:
        return None
    names = sources.loki_labels(client, uid, window)
    values = {name: sources.loki_label_values(client, uid, name, window) for name in names}
    label = loki.pick_match_all_label(values)
    if label is None:
        raise ValidationError(
            f"datasource {ds.get('name')!r} has no labels at all in this window "
            f"({window.describe()['start']}..{window.describe()['end']}) — there is nothing to build "
            f"a query from. Try a wider --since, or pass --label to target a stream directly."
        )
    return label


def _run_pass(
    client, uid: str, query: str, *, start_ns: int, end_ns: int, limit: int, name: str
) -> tuple[list[dict], dict]:
    """Run one Loki `query_range` pass; report failure rather than aborting the scan.

    A dead datasource surfacing mid-scan (the regex pass timing out on an
    overloaded Loki, say) must not blank out whatever the OTHER pass already
    found — the same reasoning `sources.survey` uses for capturing per-datasource
    errors instead of raising. Ends in `except OpError`, not `except ApiError`:
    `NotFoundError` and `ApiError` are SIBLINGS in this taxonomy (both subclass
    `OpError` directly, see errors.py), so a ladder that only caught `ApiError`
    would look exhaustive and let a bad-path 404 through the proxy escape
    uncaught — exactly the trap that leaked a raw error out of drone's
    `server doctor` before it was tightened to this floor.
    """
    meta: dict[str, Any] = {"pass": name, "logql": query, "limit": limit, "ok": True, "error": None}
    try:
        payload = client.ds_proxy(
            uid,
            "loki/api/v1/query_range",
            params={"query": query, "start": start_ns, "end": end_ns, "limit": limit, "direction": "backward"},
        )
    except OpError as exc:
        meta["ok"] = False
        meta["error"] = str(exc)
        return [], meta
    return loki.parse_streams(payload), meta


def _merge(*groups: list[dict], limit: int) -> list[dict]:
    """Combine passes, drop exact repeats, keep the newest `limit` lines.

    The level=error and severity-regex passes overlap ON PURPOSE (a panic
    logged at error level matches both), so the same line legitimately arrives
    twice. Keying on timestamp + line + the full label set — not just the line
    text — is what stops that de-dup from ALSO folding together two genuinely
    different streams that happened to log the same message in the same
    nanosecond.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for rec in itertools.chain(*groups):
        key = (rec.get("timestamp"), rec.get("line"), tuple(sorted((rec.get("labels") or {}).items())))
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    out.sort(key=lambda r: r.get("timestamp") or 0, reverse=True)
    return out[:limit] if limit else out


def _next_command(ds: dict, matchers: dict[str, str], finding: dict) -> str:
    """A ready-to-run next command for THIS finding — one with nowhere to go is a dead end.

    Localised to exactly one source (`sourceCount == 1`) -> `logs similar`,
    which exists for precisely this question ("has this happened elsewhere?",
    see analysis.to_regex): point it at the one stream this fired on. Spread
    across several sources already -> that question is already answered, so
    this hands back `logs query` with the fingerprint turned into a regex over
    the SAME scope this scan used, rather than making the agent re-derive it.

    Flag names (`--datasource`, `--label`) match this CLI's established
    convention (`_shared.parse_label_args`, `commands/context.py`); the exact
    surface of `logs similar`/`logs query` is owned by a sibling module and was
    not available to verify against at the time this was written — see the
    handoff notes.
    """
    uid = str(ds.get("uid"))
    example = (finding.get("examples") or [""])[0]
    if finding.get("sourceCount") == 1 and finding.get("sources"):
        source = str(finding["sources"][0]["source"])
        key, _, value = source.partition("=")
        return (
            f"grafana-cli logs similar {shlex.quote(example)} "
            f"--datasource {shlex.quote(uid)} --label {key}={shlex.quote(value)}"
        )
    parts = [f"grafana-cli logs query --datasource {shlex.quote(uid)}"]
    for k, v in matchers.items():
        parts.append(f"--label {k}={shlex.quote(v)}")
    regex = analysis.to_regex(str(finding.get("fingerprint") or ""))
    parts.append(f"--regex {shlex.quote(regex)}")
    return " ".join(parts)
