"""LogQL construction and Loki response parsing — hermetic.

No Loki required, which is the point: the query builder is where the subtle,
silent mistakes live (an empty selector, a `detected_level` in the wrong clause,
an unescaped quote), and every one of them is testable as a pure string.
"""

from __future__ import annotations

import pytest
from agentcli.errors import ValidationError

from grafanacli.loki import (
    build_query,
    build_selector,
    escape_value,
    parse_label_values,
    parse_streams,
    pick_match_all_label,
    summarise_labels,
)


# ---- selectors -------------------------------------------------------

def test_simple_equality_selector():
    assert build_selector({"systemd_unit": "docker.service"}) == '{systemd_unit="docker.service"}'


def test_multiple_matchers_are_comma_joined():
    q = build_selector({"hostname": "web3", "job": "systemd-journal"})
    assert q.startswith("{") and q.endswith("}")
    assert 'hostname="web3"' in q and 'job="systemd-journal"' in q


@pytest.mark.parametrize(
    "value,expected",
    [
        ("~web.*", 'hostname=~"web.*"'),
        ("=~web.*", 'hostname=~"web.*"'),
        ("!web3", 'hostname!="web3"'),
        ("!=web3", 'hostname!="web3"'),
        ("!~web.*", 'hostname!~"web.*"'),
        ("plain", 'hostname="plain"'),
    ],
)
def test_operator_prefixes_on_values(value, expected):
    assert build_selector({"hostname": value}) == "{" + expected + "}"


def test_empty_selector_is_refused_not_emitted():
    """Loki rejects `{}` with a parse error, and a parse error surfaced from three
    layers down reads like a bug in this tool."""
    with pytest.raises(ValidationError) as e:
        build_selector({})
    assert "logs sources" in str(e.value), "the error must point at the way out"


def test_match_all_needs_a_label_because_loki_has_no_wildcard():
    assert build_selector({}, match_all_label="hostname") == '{hostname=~".+"}'


def test_explicit_matchers_beat_the_match_all_fallback():
    assert build_selector({"job": "x"}, match_all_label="hostname") == '{job="x"}'


@pytest.mark.parametrize("bad", ["9lives", "has-dash", "has space", "has.dot", ""])
def test_invalid_label_names_are_rejected(bad):
    with pytest.raises(ValidationError):
        build_selector({bad: "x"})


def test_quotes_and_backslashes_are_escaped():
    """Escaping order matters: backslash first, or you escape your own escapes.
    Unit names and k8s annotations really do carry quotes."""
    assert escape_value('say "hi"') == 'say \\"hi\\"'
    assert escape_value("back\\slash") == "back\\\\slash"
    assert escape_value('a\\"b') == 'a\\\\\\"b'


def test_escaping_survives_into_the_selector():
    assert build_selector({"unit": 'we"ird'}) == '{unit="we\\"ird"}'


# ---- full queries ----------------------------------------------------

def test_contains_becomes_a_line_filter():
    q = build_query({"job": "x"}, contains=["timeout"])
    assert q == '{job="x"} |= "timeout"'


def test_multiple_filters_chain_in_order():
    q = build_query({"job": "x"}, contains=["a", "b"], excludes=["c"])
    assert q == '{job="x"} |= "a" |= "b" != "c"'


def test_regex_filter():
    q = build_query({"job": "x"}, regex="conn.*refused")
    assert q == '{job="x"} |~ "conn.*refused"'


def test_invalid_regex_fails_here_not_400_rungs_later():
    with pytest.raises(ValidationError) as e:
        build_query({"job": "x"}, regex="[unclosed")
    assert "invalid regex" in str(e.value)


def test_level_is_a_pipeline_stage_not_a_selector():
    """THE Loki trap. The derived level label is absent from the index, so
    `{detected_level="error"}` matches NOTHING while `... | detected_level="error"`
    works. Verified live, both ways."""
    q = build_query({"job": "x"}, level="error")
    assert q == '{job="x"} | detected_level="error" or level="error"'
    assert "{detected_level" not in q
    assert "{level" not in q


def test_level_filters_on_BOTH_derived_label_names():
    """Measured across two servers: Loki 3.0.0 derives `level`, newer Loki derives
    `detected_level`. Hardcoding either returns an empty SUCCESS on the other --
    indistinguishable from "there are no errors", which is the worst failure this
    tool can have. So we union them."""
    q = build_query({"job": "x"}, level="error")
    assert 'detected_level="error"' in q
    assert 'level="error"' in q
    assert " or " in q


def test_level_composes_with_line_filters_in_the_right_order():
    q = build_query({"job": "x"}, contains=["boom"], level="error")
    assert q == '{job="x"} |= "boom" | detected_level="error" or level="error"'


def test_level_value_is_escaped_in_every_clause():
    """A quote in one clause and not the other would be a parse error, and the
    union makes it easy to escape one and forget the other."""
    q = build_query({"job": "x"}, level='we"ird')
    assert q.count('we\\"ird') == 2


def test_level_alone_still_needs_a_selector():
    with pytest.raises(ValidationError):
        build_query({}, level="error")


def test_level_with_match_all():
    assert build_query({}, level="error", match_all_label="hostname") == (
        '{hostname=~".+"} | detected_level="error" or level="error"'
    )


# ---- parsing ---------------------------------------------------------

_PAYLOAD = {
    "status": "success",
    "data": {
        "resultType": "streams",
        "result": [
            {
                "stream": {"hostname": "web3", "systemd_unit": "docker.service"},
                "values": [["1784278682956119000", "level=error msg=boom"],
                           ["1784278600000000000", "level=info msg=fine"]],
            },
            {
                "stream": {"hostname": "web1", "systemd_unit": "api.service"},
                "values": [["1784278700000000000", "newest line"]],
            },
        ],
    },
}


def test_parse_streams_flattens_and_carries_labels_down():
    """Loki returns one entry per stream with a list of pairs — convenient for
    Loki, useless for a reader. One record per line, labels attached."""
    records = parse_streams(_PAYLOAD)
    assert len(records) == 3
    assert all(set(r) == {"timestamp", "time", "line", "labels"} for r in records)
    assert records[0]["labels"]["hostname"] == "web1"


def test_parse_streams_sorts_newest_first_across_streams():
    """Loki orders within a stream, not across them. `--limit 10` must mean the
    ten most recent lines overall, not ten from whichever stream came back first."""
    records = parse_streams(_PAYLOAD)
    assert [r["line"] for r in records] == ["newest line", "level=error msg=boom", "level=info msg=fine"]


def test_parse_streams_renders_iso_time_alongside_nanos():
    records = parse_streams(_PAYLOAD)
    assert records[0]["timestamp"] == 1784278700000000000
    assert records[0]["time"].startswith("2026-07-") and records[0]["time"].endswith("Z")


@pytest.mark.parametrize(
    "junk",
    [
        None, {}, [], "text",
        {"data": None},
        {"data": {"result": None}},
        {"data": {"result": [{"stream": {}, "values": [["not-a-number", "x"]]}]}},
        {"data": {"result": [{"stream": {}, "values": [["123"]]}]}},
        {"data": {"result": ["not-a-dict"]}},
    ],
)
def test_parse_streams_never_explodes_on_junk(junk):
    """A malformed payload must not be a traceback. The proxy tunnels somebody
    else's server, so 'valid JSON, unexpected shape' is a real thing that arrives."""
    assert parse_streams(junk) == []


def test_parse_label_values():
    assert parse_label_values({"status": "success", "data": ["a", "b"]}) == ["a", "b"]


@pytest.mark.parametrize("junk", [None, {}, {"data": None}, {"data": "x"}, "text"])
def test_parse_label_values_on_junk_is_empty_not_an_error(junk):
    """Loki returns an absent `data` for a label with no values in the window.
    That is normal, not broken."""
    assert parse_label_values(junk) == []


# ---- the cardinality report -----------------------------------------

_LABELS = {
    "hostname": [f"host{i}" for i in range(21)],
    "systemd_unit": [f"unit{i}.service" for i in range(87)],
    "job": ["systemd-journal"],
    "service_name": ["systemd-journal"],
}


def test_summarise_labels_ranks_by_cardinality():
    """Mirrors the live instance exactly: 87 / 21 / 1 / 1."""
    rows = summarise_labels(_LABELS)
    assert [r["label"] for r in rows] == ["systemd_unit", "hostname", "job", "service_name"]
    assert [r["values"] for r in rows] == [87, 21, 1, 1]


def test_single_value_labels_are_marked_useless():
    """The whole reason this report counts values instead of listing names: two of
    the four labels on the live instance cannot narrow anything down, and calling
    them filters is noise dressed up as help."""
    rows = {r["label"]: r for r in summarise_labels(_LABELS)}
    assert rows["systemd_unit"]["useful"] is True
    assert rows["hostname"]["useful"] is True
    assert rows["job"]["useful"] is False
    assert rows["service_name"]["useful"] is False


def test_summarise_samples_are_capped_and_stable():
    rows = {r["label"]: r for r in summarise_labels(_LABELS, sample=3)}
    assert len(rows["systemd_unit"]["sample"]) == 3
    assert rows["systemd_unit"]["sample"] == sorted(rows["systemd_unit"]["sample"])


def test_pick_match_all_label_prefers_highest_cardinality():
    """A match-all selector only returns streams that CARRY the label, so a rare
    label silently drops every stream missing it. Highest cardinality is the best
    available proxy for 'present everywhere'."""
    assert pick_match_all_label(_LABELS) == "systemd_unit"


def test_pick_match_all_label_on_nothing():
    assert pick_match_all_label({}) is None


def test_pick_match_all_label_is_deterministic_on_a_tie():
    """Ties break by name, so the query a command builds does not change between
    runs — an unstable query is unreproducible support."""
    assert pick_match_all_label({"b": ["1"], "a": ["1"]}) == "b"
