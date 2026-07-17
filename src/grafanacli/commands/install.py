"""`grafana-cli install claude` — register this CLI with Claude Code.

The idiomatic way to make a CLI discoverable to Claude Code is a **Skill**: a
``SKILL.md`` whose ``description`` tells Claude when to use the tool. This
command drops that skill into ``~/.claude/skills/grafana/`` (or the project's
``.claude/skills/``), and can optionally add a one-line hint to the user's
``~/.claude/CLAUDE.md`` memory. Everything is reversible with ``--uninstall``.

The machinery here is near-verbatim from the sibling CLIs — same function names
(`claude_available`, `skill_installed`, `write_skill`), because `appctx.py`'s
first-run offer calls them by name and a rename would silently break that offer
without either side raising. What is fresh is `SKILL_MD` itself: every trigger
word is anchored to "Grafana" (bare "logs"/"dashboard"/"alert" would fire on any
unrelated question), and only commands that actually exist in this tree are
named — a sibling tool once shipped a SKILL.md pointing at a `guide gotchas`
topic that did not exist, which is a `grafana-cli guide` exit 2 for the first agent that
tries it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer

from .. import __version__
from ..errors import OpError
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)

SKILL_NAME = "grafana"
_MEM_START = "<!-- grafana-cli:start -->"
_MEM_END = "<!-- grafana-cli:end -->"

# The `description` is the ENTIRE matching surface — Claude sees this and
# nothing else when deciding whether to load the skill. Every trigger is
# anchored to "Grafana" (or a Grafana-specific noun: "Loki logs", "a Grafana
# dashboard", "a Grafana alert"). Bare "logs", "dashboard", "alert" or
# "metrics" would fire this skill on any unrelated question carrying those
# words, and an over-firing skill gets distrusted and stops being loaded at all.
SKILL_MD = f"""\
---
name: grafana
description: >-
  Work with Grafana via the `grafana-cli` command — discover which Grafana datasources
  carry Grafana logs, query Loki logs, tail Grafana logs live, search and
  cluster Grafana logs to find similar problems, run PromQL against a Grafana
  Prometheus/Mimir datasource, list and read Grafana dashboards, create a
  Grafana dashboard, list and create Grafana alert rules, check whether a
  firing Grafana alert will actually notify anyone, and inspect Grafana
  datasources, Grafana organisations and Grafana notification policies. Use
  this whenever the user mentions Grafana, a Grafana dashboard, Loki logs, a
  Grafana alert, Grafana notifications, or wants to query or change their
  Grafana server.
---

# Grafana CLI (agent-tool-grafana-cli v{__version__})

The `grafana-cli` command is installed on this machine and talks to the user's
Grafana server over its REST API.

## Start here: you cannot query what you cannot name

Grafana will not tell you "what can I get logs from?" anywhere in its UI or
API — you are expected to already know a datasource's uid. `grafana-cli logs sources`
derives the answer instead: every log-capable datasource, with each label's
name AND how many values it has (a label with one value cannot filter
anything). Run this before any `grafana-cli logs query` — guessing or reusing a uid
from a different profile queries the wrong org's logs, silently.

## The workflow this tool is for

    grafana-cli scan                                          # is anything broken right now?
    grafana-cli logs sources                                   # what CAN I get logs from?
    grafana-cli logs query --datasource <uid> --level error --since 1h
    grafana-cli alert create ...                                # so you hear about it next time

## Learn the tool from the tool
- `grafana-cli guide` — the built-in operating manual, with its own topic list.
- `grafana-cli <group> --help` for any command.

## Commands
- `grafana-cli guide` — the manual. `grafana-cli scan` — one-pass health check.
- `grafana-cli logs sources|query|tail|search|similar|levels` — discovery, query,
  live tail, full-text search, "find lines like this one", level breakdown.
- `grafana-cli metrics query|list|describe|labels|up` — PromQL against
  Prometheus/Mimir.
- `grafana-cli dashboard list|search|get|panels|create|delete|folders`
- `grafana-cli alert list|get|firing|route|create|delete|pause|unpause` — `route`
  answers "will this rule actually notify anyone?" before it fires for real.
- `grafana-cli notify list|policies|check|silences` — contact points,
  notification policies, silences.
- `grafana-cli datasource list|get|health|test`
- `grafana-cli org current|list|check`
- `grafana-cli auth login|status|logout`
- `grafana-cli server health|version|doctor`
- `grafana-cli raw get|post|put|patch|delete <path>` — escape hatch, any endpoint.
- `grafana-cli settings`, `grafana-cli context`, `grafana-cli install claude`.

## Output contract
- Default output is JSON on stdout — parse it.
- **Exception: log LINES are prose, not JSON** (`grafana-cli logs query`, `grafana-cli logs
  tail`, `grafana-cli logs search`) — do not `json.loads` a log line.
- Errors are JSON on **stderr** with a non-zero exit code.
- Trim with `--fields uid,name,type`; `-o table` for humans; `-o csv` to export.

## Exit codes
`0` ok · `1` generic · `3` config · `4` auth · `5` not found · `6` conflict ·
`7` validation · `8` a datasource (Loki/Prometheus) is unreachable even though
Grafana itself answered · `9` the token belongs to the wrong Grafana org ·
`130` interrupted.

## Auth — one profile per Grafana organisation
A Grafana service-account token is hard-scoped to a single org; there is no
header or flag that widens it. Multi-org therefore means multiple profiles,
one token each:

    grafana-cli auth login                          # profile "default"
    grafana-cli auth login --profile sales          # a second org, a second token
    grafana-cli -p sales logs sources

If not configured, ask the user to run `grafana-cli auth login` (or set GRAFANA_URL +
GRAFANA_TOKEN). Check with `grafana-cli auth status` — it names which token/backend is
actually in use, since an exported GRAFANA_TOKEN silently overrides a keyring
login.

## Make changes safely
Preview ANY write with a global `--dry-run` (prints the exact request, sends
nothing). Confirm destructive actions (delete a dashboard, delete an alert
rule) with the user first.

## Stop repeating --datasource
`grafana-cli context set --datasource <uid>` makes it the default for later commands
— but only for the profile active when you set it. Context is scoped PER
PROFILE on purpose: a datasource uid from one org does not exist in another,
so this tool never lets a stale default from org A 404 against org B. If
output looks wrongly scoped, run `grafana-cli context show` or pass `--no-context`.

## Gotchas that bite once
- Loki's own timestamps are nanoseconds through the datasource proxy; this CLI
  handles that for you. If you drop to `grafana-cli raw`, remember it.
- `detected_level` is filterable on a log query but does NOT appear in the
  label list — Loki derives it at query time, not at index time.
- A firing Grafana alert with zero notification integrations notifies NOBODY.
  `grafana-cli alert route` / `grafana-cli notify check` say so explicitly; a bare "firing"
  status in `grafana-cli alert firing` does not mean anyone was told.
- Listing datasources needs `datasources:read`, which Viewer has. Only WRITES
  need Editor. An Editor token can query a datasource
  it already knows the uid of, but `grafana-cli logs sources` — discovery — needs
  Admin; that is why `grafana-cli auth login` asks for an Admin-role token.

Anything not wrapped: `grafana-cli raw get|post|put|patch|delete <path>` (paths are
relative to `<server>/api`; reaching a datasource's own API, e.g. Loki, needs
the proxy path — see `grafana-cli raw --help`).
"""

_MEMORY_HINT = (
    f"{_MEM_START}\n"
    "The `grafana-cli` CLI (package agent-tool-grafana-cli) is installed. It is an "
    "agent-ready Grafana client with JSON output — `grafana-cli logs sources` answers "
    "'what can I get logs from?'. Run `grafana-cli guide` to learn it.\n"
    f"{_MEM_END}\n"
)


def claude_available() -> bool:
    """Best-effort: is Claude Code installed on this machine?

    Note this detects *Claude*, not Grafana — unchanged from the sibling CLIs
    on purpose; the same check answers the same question everywhere.
    """
    if shutil.which("claude"):
        return True
    home = Path.home()
    return (home / ".claude").is_dir() or (home / ".local" / "bin" / "claude").exists()


def _skill_dir(project: bool) -> Path:
    base = Path.cwd() if project else Path.home()
    return base / ".claude" / "skills" / SKILL_NAME


def skill_installed(project: bool = False) -> bool:
    return (_skill_dir(project) / "SKILL.md").exists()


def write_skill(project: bool = False) -> Path:
    d = _skill_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "SKILL.md"
    # Explicit encoding: a generated file with no encoding declared falls back
    # to the platform default, which on Windows is not UTF-8 — and SKILL_MD's
    # prose leans on real em dashes, not the ASCII "--" substitute.
    path.write_text(SKILL_MD, encoding="utf-8")
    return path


def _memory_file() -> Path:
    return Path.home() / ".claude" / "CLAUDE.md"


def write_memory_hint() -> Path:
    path = _memory_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if _MEM_START in existing:
        return path  # already present
    sep = "" if existing.endswith("\n") or not existing else "\n"
    path.write_text(existing + sep + "\n" + _MEMORY_HINT, encoding="utf-8")
    return path


def _remove_memory_hint() -> bool:
    """Remove only our marked block — the file is the user's, and everything
    outside the markers is theirs, not ours to rewrite."""
    path = _memory_file()
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if _MEM_START not in text or _MEM_END not in text:
        return False
    before, _, rest = text.partition(_MEM_START)
    _, _, after = rest.partition(_MEM_END)
    path.write_text((before.rstrip("\n") + "\n" + after.lstrip("\n")).strip("\n") + "\n", encoding="utf-8")
    return True


@app.command()
def claude(
    ctx: typer.Context,
    project: bool = typer.Option(
        False, "--project", help="Install into ./.claude (this repo) instead of ~/.claude."
    ),
    memory: bool = typer.Option(False, "--memory", help="Also add a one-line hint to ~/.claude/CLAUDE.md."),
    force: bool = typer.Option(False, "--force", help="Install even if Claude Code isn't detected."),
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove the skill (and memory hint)."),
    print_: bool = typer.Option(False, "--print", help="Print the SKILL.md that would be written and exit."),
) -> None:
    """Register this CLI with Claude Code as a Skill so Claude auto-uses it.

    Writes ~/.claude/skills/grafana/SKILL.md (idiomatic discovery). Claude then
    invokes `grafana-cli` whenever you mention Grafana, Loki logs or dashboards.
    Reversible with --uninstall.
    """
    obj = ctx_obj(ctx)

    if print_:
        typer.echo(SKILL_MD)
        return

    if uninstall:
        d = _skill_dir(project)
        removed = []
        if (d / "SKILL.md").exists():
            (d / "SKILL.md").unlink()
            try:
                d.rmdir()
            except OSError:
                pass  # the user put other files there; leaving them is correct
            removed.append(str(d))
        if _remove_memory_hint():
            removed.append(str(_memory_file()) + " (hint)")
        obj.emitter.emit({"status": "uninstalled", "removed": removed})
        return

    if not force and not claude_available():
        raise OpError(
            "Claude Code was not detected on this machine. Install it from "
            "https://claude.com/claude-code, or re-run with --force to install the skill anyway."
        )

    skill_path = write_skill(project)
    result = {
        "status": "installed",
        "skill": str(skill_path),
        "scope": "project" if project else "user",
        "note": (
            "Claude Code will use the `grafana-cli` CLI automatically when you mention "
            "Grafana. Start a new session to pick it up."
        ),
    }
    if memory:
        result["memoryHint"] = str(write_memory_hint())
    obj.emitter.emit(result)
