"""Notification routing — hermetic.

These tests carry more weight than most: `alert route` tells a human "yes, this
will page you", and a *nearly* correct routing model is worse than none, because
it is confidently wrong about the one thing you asked. So the walk is tested
against Alertmanager's actual semantics, edge by edge.

The `delivery_report` tests at the bottom encode the live finding this feature
exists for: nine alerts firing into a receiver with zero integrations.
"""

from __future__ import annotations

import pytest

from grafanacli.routing import (
    Matcher,
    delivery_report,
    integration_names,
    parse_matchers,
    resolve_receivers,
    rule_labels,
)


# ---- matchers --------------------------------------------------------

@pytest.mark.parametrize(
    "op,value,labels,expected",
    [
        ("=", "critical", {"severity": "critical"}, True),
        ("=", "critical", {"severity": "warning"}, False),
        ("!=", "critical", {"severity": "warning"}, True),
        ("!=", "critical", {"severity": "critical"}, False),
        ("=~", "crit.*", {"severity": "critical"}, True),
        ("=~", "crit.*", {"severity": "warning"}, False),
        ("!~", "crit.*", {"severity": "warning"}, True),
        ("!~", "crit.*", {"severity": "critical"}, False),
    ],
)
def test_matcher_operators(op, value, labels, expected):
    assert Matcher("severity", op, value).matches(labels) is expected


def test_missing_label_is_the_empty_string():
    """Alertmanager's rule, and the reason `severity!="critical"` matches an alert
    with no severity at all. Treating absent as 'no match' would quietly drop
    routes — the failure mode where you learn about it during an incident."""
    assert Matcher("severity", "=", "").matches({}) is True
    assert Matcher("severity", "!=", "critical").matches({}) is True
    assert Matcher("severity", "=", "critical").matches({}) is False


def test_regex_matchers_are_anchored_both_ends():
    """Alertmanager anchors; an unanchored port would match far too much and name
    the wrong receiver."""
    assert Matcher("team", "=~", "back").matches({"team": "back"}) is True
    # The point of anchoring: "back" must not match "backend" as a prefix...
    assert Matcher("team", "=~", "back").matches({"team": "backend"}) is False
    # ...while an explicit wildcard still does.
    assert Matcher("team", "=~", "back.*").matches({"team": "backend"}) is True
    # And an anchored pattern must not match a suffix either.
    assert Matcher("team", "=~", "end").matches({"team": "backend"}) is False


def test_unparseable_regex_does_not_silently_match_everything():
    """A broken matcher must not become a wildcard that swallows every alert into
    the wrong branch."""
    assert Matcher("team", "=~", "[unclosed").matches({"team": "anything"}) is False
    assert Matcher("team", "!~", "[unclosed").matches({"team": "anything"}) is True


def test_unknown_operator_is_rejected():
    with pytest.raises(ValueError):
        Matcher("a", "<>", "b")


# ---- reading the shapes Grafana has written over the years -----------

def test_parses_object_matchers():
    ms = parse_matchers({"object_matchers": [["severity", "=", "critical"]]})
    assert len(ms) == 1 and ms[0].describe() == "severity='critical'"


def test_parses_legacy_match_and_match_re():
    """Both survive in policies provisioned years ago and are still honoured by
    the server. Reading only the modern shape would treat an old route as
    'matches everything' and report the wrong receiver with total confidence."""
    ms = parse_matchers({"match": {"team": "be"}, "match_re": {"env": "pro.*"}})
    assert {m.op for m in ms} == {"=", "=~"}
    assert all(m.matches({"team": "be", "env": "prod"}) for m in ms)


def test_parses_string_matchers():
    ms = parse_matchers({"matchers": ['severity="critical"', "team=~back.*"]})
    assert len(ms) == 2
    assert ms[0].matches({"severity": "critical"})
    assert ms[1].matches({"team": "backend"})


def test_malformed_matchers_are_skipped_not_fatal():
    ms = parse_matchers({"object_matchers": [["only-two", "="], "not-a-list"], "matchers": ["garbage"]})
    assert ms == []


def test_no_matchers_means_matches_everything():
    assert parse_matchers({}) == []


# ---- the walk --------------------------------------------------------

_TREE = {
    "receiver": "Default",
    "routes": [
        {"receiver": "Pager", "object_matchers": [["severity", "=", "critical"]]},
        {"receiver": "Slack", "object_matchers": [["team", "=", "be"]], "continue": True},
        {"receiver": "Email", "object_matchers": [["team", "=", "be"]]},
    ],
}


def test_unmatched_labels_fall_through_to_the_root_receiver():
    got = resolve_receivers(_TREE, {"alertname": "whatever"})
    assert [r["receiver"] for r in got] == ["Default"]


def test_first_match_wins_and_stops():
    got = resolve_receivers(_TREE, {"severity": "critical", "team": "be"})
    assert [r["receiver"] for r in got] == ["Pager"], "critical must not also reach Slack/Email"


def test_continue_true_keeps_siblings_in_play():
    """This is how one alert reaches two receivers, and it is easy to get wrong:
    Slack sets continue, so Email must ALSO match."""
    got = resolve_receivers(_TREE, {"team": "be"})
    assert [r["receiver"] for r in got] == ["Slack", "Email"]


def test_nested_routes_descend():
    tree = {
        "receiver": "Root",
        "routes": [
            {
                "receiver": "TeamBE",
                "object_matchers": [["team", "=", "be"]],
                "routes": [{"receiver": "BEPager", "object_matchers": [["severity", "=", "critical"]]}],
            }
        ],
    }
    assert [r["receiver"] for r in resolve_receivers(tree, {"team": "be", "severity": "critical"})] == ["BEPager"]
    # Matched the parent but no child -> the parent itself is the match.
    assert [r["receiver"] for r in resolve_receivers(tree, {"team": "be"})] == ["TeamBE"]


def test_child_without_a_receiver_inherits_the_parent():
    """Alertmanager inherits down the tree rather than defaulting to nothing.
    Reporting `receiver: None` here would look like a black hole that isn't one."""
    tree = {"receiver": "Root", "routes": [{"object_matchers": [["team", "=", "be"]]}]}
    assert [r["receiver"] for r in resolve_receivers(tree, {"team": "be"})] == ["Root"]


def test_a_root_with_its_own_matchers_can_reject_outright():
    tree = {"receiver": "Root", "object_matchers": [["env", "=", "prod"]]}
    assert resolve_receivers(tree, {"env": "dev"}) == []


def test_the_path_records_how_we_got_there():
    """'Which route caught this' is the actual debugging question; a bare receiver
    name does not answer it."""
    got = resolve_receivers(_TREE, {"severity": "critical"})
    assert got[0]["path"][0] == "<root>"
    assert "severity" in got[0]["path"][1]


def test_empty_tree_does_not_crash():
    assert [r["receiver"] for r in resolve_receivers({}, {"a": "b"})] == [None]


# ---- integrations ----------------------------------------------------

def test_integration_names_reads_both_api_shapes():
    """The provisioning API says `grafana_managed_receiver_configs`, the
    alertmanager API says `integrations`. Read one and you see an empty receiver
    that is fine, or a full one that is empty."""
    assert integration_names({"integrations": [{"type": "email"}]}) == ["email"]
    assert integration_names({"grafana_managed_receiver_configs": [{"type": "slack"}]}) == ["slack"]
    assert integration_names({"integrations": []}) == []
    assert integration_names({}) == []


# ---- THE report ------------------------------------------------------

def test_the_live_black_hole_is_reported():
    """The finding this whole feature exists for, reproduced exactly: the live
    instance has nine alerts firing, all routed to a receiver named `Default`
    whose integrations array is empty. Grafana shows them as firing normally and
    nobody is told."""
    report = delivery_report(
        {"alertname": "Example Registry Alert"},
        {"receiver": "Default", "group_by": ["grafana_folder", "alertname"]},
        [{"active": True, "integrations": [], "name": "Default"}],
    )
    assert report["delivered"] is False
    assert len(report["problems"]) == 1
    assert "no integrations" in report["problems"][0]
    assert "fire into the void" in report["problems"][0]


def test_a_working_route_is_delivered():
    report = delivery_report(
        {"severity": "critical"},
        {"receiver": "Default", "routes": [{"receiver": "Pager", "object_matchers": [["severity", "=", "critical"]]}]},
        [{"name": "Pager", "integrations": [{"type": "pagerduty"}]}, {"name": "Default", "integrations": []}],
    )
    assert report["delivered"] is True
    assert report["problems"] == []
    assert report["routes"][0]["integrations"] == ["pagerduty"]


def test_a_dangling_receiver_reference_is_reported():
    """The policy names a contact point that does not exist — a different fault
    from a hollow one, and it needs different words."""
    report = delivery_report({"a": "b"}, {"receiver": "Ghost"}, [])
    assert report["delivered"] is False
    assert report["routes"][0]["exists"] is False
    assert "no such contact point exists" in report["problems"][0]


def test_a_muted_route_is_not_delivered_and_says_so():
    """Muted looks exactly like working in the API response: integrations are
    present, the route matches, nothing is wrong — and nothing arrives."""
    report = delivery_report(
        {"a": "b"},
        {"receiver": "Pager", "mute_time_intervals": ["nights"]},
        [{"name": "Pager", "integrations": [{"type": "email"}]}],
    )
    assert report["delivered"] is False
    assert "mute timing" in report["problems"][0]


def test_partial_delivery_counts_as_delivered_but_keeps_the_problem():
    """One good route and one hollow one: someone IS told, so `delivered` is true
    — but the hollow branch is still a real misconfiguration and must not vanish
    from the report just because a sibling worked."""
    report = delivery_report(
        {"team": "be"},
        {
            "receiver": "Root",
            "routes": [
                {"receiver": "Slack", "object_matchers": [["team", "=", "be"]], "continue": True},
                {"receiver": "Hollow", "object_matchers": [["team", "=", "be"]]},
            ],
        },
        [{"name": "Slack", "integrations": [{"type": "slack"}]}, {"name": "Hollow", "integrations": []}],
    )
    assert report["delivered"] is True
    assert len(report["problems"]) == 1


# ---- rule labels -----------------------------------------------------

def test_rule_labels_inject_what_grafana_injects():
    """Grafana adds `alertname` and `grafana_folder` server-side rather than
    storing them on the rule, and the DEFAULT policy groups by exactly those two.
    Routing a rule's own labels alone models the wrong alert."""
    labels = rule_labels(
        {"title": "Example Registry Alert", "folderTitle": "Platform", "labels": {"severity": "critical"}}
    )
    assert labels == {
        "severity": "critical",
        "alertname": "Example Registry Alert",
        "grafana_folder": "Platform",
    }


def test_rule_labels_fall_back_to_the_folder_uid():
    labels = rule_labels({"title": "T", "folderUID": "abc123"})
    assert labels["grafana_folder"] == "abc123"


def test_rule_labels_do_not_clobber_explicit_ones():
    labels = rule_labels({"title": "T", "labels": {"alertname": "Custom"}})
    assert labels["alertname"] == "Custom"
