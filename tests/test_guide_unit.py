"""The guide and the Claude skill must only promise commands that EXIST.

Docs that describe a CLI drift from it silently: nothing executes prose, so a
command invented in a cheat-sheet costs nothing at write time and everything at
read time. Two live defects in the sibling tool motivated this file, both found
by hand rather than by CI:

  * its guide advertised a `fields` command under DISCOVER. No such command was
    ever implemented — the discovery trio was planned, documented, never built.
  * its installed SKILL.md pointed Claude at a `gotchas` topic that did not
    exist, so it exited 2 — and `gotchas` is the most inviting name on the list,
    so an agent reaches for it FIRST and the only broken one is the one it tries.

Both are the same bug: text making a promise the tree does not keep. So these
tests resolve every `grafana-cli ...` string in the guide and the skill against the REAL
Typer tree, and cross-check the advertised topic list against the topics that
exist.

Hermetic: introspection only. No network, no client, no config.
"""

from __future__ import annotations

import re

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from grafanacli.cli import app
from grafanacli.commands import guide as G
from grafanacli.commands import install as I


def _walk(cmd, path: str = ""):
    """Yield the path of every leaf command.

    Duck-typed on `commands` rather than isinstance(click.Group): Typer vendors
    Click privately (`typer._click`) and has moved it before.
    """
    subs = getattr(cmd, "commands", None)
    if subs:
        for name, sub in subs.items():
            yield from _walk(sub, f"{path} {name}".strip())
    elif path:
        yield path


def _real_commands() -> set[str]:
    return set(_walk(get_command(app)))


def _groups() -> set[str]:
    """Group names are legitimate targets too (`grafana-cli logs --help`)."""
    return {p.split()[0] for p in _real_commands()}


#: Words that follow `grafana-cli` in prose but are not commands: flags, placeholders,
#: and the global options handled by the root callback.
_NOT_A_COMMAND = re.compile(r"^(-|<|\.\.\.|\||$)")

#: `grafana-cli <thing>` or `grafana-cli -p x <thing> <sub>`. Captures up to two words so
#: `grafana-cli logs sources` resolves as a path, not just `logs`.
_INVOCATION = re.compile(r"\bgraf\s+((?:-\w+\s+\S+\s+)*)([a-z][\w-]*)(?:\s+([a-z][\w-]*))?")


def _invocations(text: str) -> set[str]:
    found = set()
    for _opts, first, second in _INVOCATION.findall(text):
        if _NOT_A_COMMAND.match(first):
            continue
        if second and not _NOT_A_COMMAND.match(second):
            found.add(f"{first} {second}")
        else:
            found.add(first)
    return found


def _resolves(candidate: str, real: set[str], groups: set[str]) -> bool:
    if candidate in real or candidate in groups:
        return True
    # "grafana-cli logs query" where the doc wrote "grafana-cli logs query --raw": the two-word
    # path is what we captured, so a prefix match against a real leaf is enough.
    if any(leaf == candidate or leaf.startswith(candidate + " ") for leaf in real):
        return True
    # `grafana-cli guide gotchas` / `grafana-cli scan --since 15m`: a top-level LEAF command
    # takes ARGUMENTS, so the second word we captured is an argument, not a
    # subcommand. Resolving it as a path would be wrong -- and did produce a false
    # positive on `guide gotchas`, the very name this file exists to protect.
    head = candidate.split()[0]
    return head in real and head not in groups - real


# ---- the tree itself -------------------------------------------------

def test_the_tree_actually_has_commands():
    """Guard the guard: if introspection breaks, every test below passes
    vacuously and we learn nothing."""
    assert len(_real_commands()) > 30, f"walked only {len(_real_commands())} commands"


# ---- the guide -------------------------------------------------------

def test_every_command_named_in_the_overview_exists():
    real, groups = _real_commands(), _groups()
    broken = sorted(c for c in _invocations(G.OVERVIEW) if not _resolves(c, real, groups))
    assert not broken, (
        f"the guide's overview promises commands that do not exist: {broken}\n"
        f"An agent reads this FIRST. Either build them or stop advertising them."
    )


@pytest.mark.parametrize("topic", sorted(G.TOPICS))
def test_every_command_named_in_a_topic_exists(topic):
    real, groups = _real_commands(), _groups()
    broken = sorted(c for c in _invocations(G.TOPICS[topic]) if not _resolves(c, real, groups))
    assert not broken, f"`grafana-cli guide {topic}` promises commands that do not exist: {broken}"


def test_the_advertised_topic_list_matches_the_real_topics():
    """The `gotchas` bug exactly: the overview lists topics, and the list is prose
    that nothing checks."""
    listed = re.search(r"^TOPICS:\s*(.+?)(?:\n\n|\Z)", G.OVERVIEW, re.S | re.M)
    assert listed, "the overview must advertise its topics"
    advertised = {t.strip() for t in re.split(r"[·\n]", listed.group(1)) if t.strip()}
    assert advertised == set(G.TOPICS), (
        f"advertised but missing: {sorted(advertised - set(G.TOPICS))}; "
        f"exists but unadvertised: {sorted(set(G.TOPICS) - advertised)}"
    )


def test_the_guide_only_cross_references_topics_that_exist():
    """`_resolves` deliberately treats `guide <topic>` as command-plus-argument,
    so the command check above cannot see a bad topic name. This closes that gap
    for the guide's own text — the SKILL.md equivalent is further down, and the
    sibling tool shipped exactly this bug in exactly that file."""
    referenced = set(re.findall(r"grafana-cli guide (\w+)", G.OVERVIEW + "".join(G.TOPICS.values())))
    unknown = sorted(t for t in referenced if t not in G.TOPICS and t not in ("topics", "list"))
    assert not unknown, f"the guide cross-references topics that do not exist: {unknown}"


def test_the_gotchas_topic_exists():
    """Named explicitly because it is the one an agent reaches for first, and the
    sibling tool shipped a skill pointing at a `gotchas` topic that 404'd."""
    assert "gotchas" in G.TOPICS


def test_every_topic_is_reachable_and_exits_zero():
    runner = CliRunner()
    for topic in G.TOPICS:
        result = runner.invoke(app, ["guide", topic])
        assert result.exit_code == 0, f"`grafana-cli guide {topic}` exited {result.exit_code}"
        assert result.stdout.strip(), f"`grafana-cli guide {topic}` printed nothing"


def test_an_unknown_topic_is_a_signpost_not_a_dead_end():
    result = CliRunner().invoke(app, ["guide", "nonsense"])
    assert result.exit_code == 2
    assert "gotchas" in result.output, "a wrong turn must list the real topics"


def test_topics_listing():
    result = CliRunner().invoke(app, ["guide", "topics"])
    assert result.exit_code == 0
    assert set(result.stdout.split()) == set(G.TOPICS)


def test_the_guide_needs_no_config_no_token_no_network(monkeypatch, tmp_path):
    """THE property. An agent with only this binary runs `guide` first, and a
    human runs it when everything else is broken — so it must not fail for the
    same reasons everything else did.

    The shell equivalent, worth running by hand on the built binary:
        env -i PATH=/usr/bin:/bin HOME=/nonexistent ./grafana-cli guide
    """
    monkeypatch.setenv("GRAFANACLI_CONFIG_DIR", str(tmp_path / "does-not-exist"))
    for var in ("GRAFANA_URL", "GRAFANA_TOKEN", "GRAFANACLI_URL", "GRAFANACLI_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    result = CliRunner().invoke(app, ["guide"])
    assert result.exit_code == 0
    assert "grafana-cli" in result.stdout


def test_the_guide_documents_the_exit_codes_the_code_actually_raises():
    """An exit-code table that drifts from the code is worse than none: an agent
    branches on it."""
    from grafanacli.errors import DatasourceUnreachable, OrgMismatch

    text = G.OVERVIEW + G.TOPICS["output"]
    assert f"{DatasourceUnreachable.exit_code} datasource" in text.replace("·", " ")
    assert str(OrgMismatch.exit_code) in text


def test_the_guide_states_the_reserved_flag_rule():
    """The `--out` vs `--output` trap cost a sibling four releases. If it is not
    in the guide, an agent will reach for --output and silently write the wrong
    file."""
    assert "--out" in G.TOPICS["output"]
    assert "--output" in G.TOPICS["output"]


# ---- the Claude skill ------------------------------------------------

def test_every_command_named_in_the_skill_exists():
    real, groups = _real_commands(), _groups()
    broken = sorted(c for c in _invocations(I.SKILL_MD) if not _resolves(c, real, groups))
    assert not broken, (
        f"SKILL.md promises commands that do not exist: {broken}\n"
        f"This is what Claude reads to decide how to drive the tool."
    )


def test_the_skill_only_points_at_topics_that_exist():
    """The exact sibling defect: a skill pointing at `guide gotchas` when no such
    topic existed, so the first thing an agent tried exited 2."""
    referenced = set(re.findall(r"grafana-cli guide (\w+)", I.SKILL_MD))
    unknown = sorted(t for t in referenced if t not in G.TOPICS and t not in ("topics", "list"))
    assert not unknown, f"SKILL.md points at nonexistent guide topics: {unknown}"


def test_the_skill_anchors_its_triggers_to_grafana():
    """Bare 'logs' or 'dashboard' over-fires on every conversation. Every trigger
    has to name the product or the tool fires on someone's git log."""
    lowered = I.SKILL_MD.lower()
    assert "grafana" in lowered
    for bare in re.findall(r"^\s*[-*]\s+\"?(logs|dashboard|alert)\"?\s*$", I.SKILL_MD, re.M | re.I):
        pytest.fail(f"SKILL.md has an unanchored trigger word: {bare!r}")
