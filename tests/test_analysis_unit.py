"""Fingerprinting, clustering and classification — hermetic.

The engine behind `graf scan` and `graf logs similar`. Everything here is a pure
function from strings to data, so it is testable exactly, and it needs to be: a
classifier that over-claims turns "any irregularities?" into a false alarm, and
one that under-claims hides a panic.
"""

from __future__ import annotations

import pytest

from grafanacli.analysis import (
    classify,
    cluster,
    detect_format,
    fingerprint,
    looks_like_json,
    summarise,
    to_regex,
    verdict,
)


# ---- fingerprinting --------------------------------------------------

def test_the_core_collapse():
    """Two lines, one problem. This single behaviour is what makes "anything
    irregular?" answerable in a context window instead of a log dump."""
    a = fingerprint("connection to 10.0.0.7:5432 failed after 1.2s")
    b = fingerprint("connection to 10.0.0.9:5432 failed after 0.4s")
    assert a == b
    assert a == "connection to <ADDR> failed after <DUR>"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("2026-07-17T08:53:26.956094830+02:00 boom", "<TS> boom"),
        ("2026-07-17 08:53:26 boom", "<TS> boom"),
        ("Jul 17 08:53:26 boom", "<TS> boom"),
        ("req 550e8400-e29b-41d4-a716-446655440000 failed", "req <UUID> failed"),
        ("addr 192.168.1.1 down", "addr <ADDR> down"),
        ("addr 192.168.1.1:8080 down", "addr <ADDR> down"),
        ("ptr 0xdeadbeef", "ptr <HEX>"),
        ("commit 42f7a46ed69c9cdd", "commit <HEX>"),
        ("took 1.234ms", "took <DUR>"),
        ("took 15s", "took <DUR>"),
        ("used 512MB", "used <SIZE>"),
        ("at 95%", "at <SIZE>"),
        ("retry 5", "retry <N>"),
        ("  spaced   out  ", "spaced out"),
    ],
)
def test_normalisers(line, expected):
    assert fingerprint(line) == expected


def test_uuid_is_eaten_before_the_number_rule():
    """Ordering guard. If the bare-number rule ran first it would chew the UUID's
    digits and leave the dashes, so every id would look distinct and nothing would
    ever cluster."""
    assert fingerprint("id 550e8400-e29b-41d4-a716-446655440000") == "id <UUID>"


def test_duration_is_eaten_before_the_number_rule():
    assert fingerprint("took 1.2s") == "took <DUR>"
    assert fingerprint("took 1.2s") != "took <N>s"


def test_opaque_alphanumeric_ids_collapse():
    """Regression, found live and not by review. A `scan` of a real Docker Swarm
    reported **204 distinct problems out of 256 lines** that were all one problem
    — because every line carried `node.id=`/`service.id=`/`task.id=`, and those
    ids are base32-ish, not hex, so the <HEX> rule could not touch them. Every
    line got a unique fingerprint and the whole point of clustering was lost.

    Modern orchestrators (Swarm, Kubernetes, Nomad) and modern id schemes (ULID,
    nanoid, base62) all emit non-hex ids, so without this the fingerprinter
    silently stops collapsing most real-world logs. Shape copied from the live
    line; the registry host is scrubbed.
    """
    a = ('time="2026-07-17T11:36:36.411034844+02:00" level=error msg="fatal task error" '
         'error="No such image: registry.example.com/app/app:c1f0382b0f" '
         'module=node/agent/taskmanager node.id=sxbx71pvrl5cbzizfk5i4e9u5 '
         'service.id=tt4mexn4umfcyjoscvprpdz33 task.id=54syhoorqyabcdefghijklmno')
    b = ('time="2026-07-17T11:36:21.389811379+02:00" level=error msg="fatal task error" '
         'error="No such image: registry.example.com/app/app:9ab3c7d1e2" '
         'module=node/agent/taskmanager node.id=zzzz71pvrl5cbzizfk5i4e9u5 '
         'service.id=qq4mexn4umfcyjoscvprpdz33 task.id=11syhoorqyabcdefghijklmno')
    assert fingerprint(a) == fingerprint(b)
    assert "<ID>" in fingerprint(a)
    assert cluster([_rec(a, ts=1), _rec(b, ts=2)])[0]["count"] == 2


@pytest.mark.parametrize(
    "word",
    [
        "taskmanager", "authentication", "internationalisation",
        "responsibilities", "NotImplementedException",
    ],
)
def test_the_id_rule_does_not_eat_long_english(word):
    """The guard that keeps <ID> honest: it needs BOTH a letter and a digit, so a
    long word -- however long -- survives. Without this the fingerprint stops
    being readable and the reader has to trust a signature they cannot check."""
    assert word in fingerprint(f"the {word} failed")


def test_the_id_rule_leaves_short_tokens_alone():
    """16 is the floor. Kubernetes' short pod suffixes (`x4k2p`) are below it and
    stay — a documented limitation, and the safe direction: eating a short token
    risks eating real words."""
    assert fingerprint("pod nginx-x4k2p died") == "pod nginx-x4k2p died"


def test_a_git_sha_still_reads_as_hex_not_id():
    """Ordering guard: <HEX> runs first, so a sha keeps its more specific label."""
    assert fingerprint("commit 42f7a46ed69c9cdd53a6b44fe98c0d55986b19a5") == "commit <HEX>"


def test_short_hex_is_not_eaten_out_of_english():
    """The 7-char floor is git's short-sha length. Lower it and the rule starts
    eating words like 'decade' and 'faceted', and the fingerprint stops being
    readable — the reader has to trust a signature they cannot check."""
    assert "decade" in fingerprint("a decade of uptime")


def test_distinct_problems_do_not_collapse():
    """The other failure direction: over-normalising merges unrelated problems
    into one finding and hides a real fault behind a bigger one."""
    assert fingerprint("disk full on /var") != fingerprint("connection refused")


def test_fingerprint_is_stable_and_total():
    assert fingerprint("") == ""
    assert fingerprint(None) == ""


# ---- classification --------------------------------------------------

@pytest.mark.parametrize(
    "line,expected",
    [
        ("panic: runtime error: invalid memory address", "panic"),
        ("goroutine 42 [running]:", "panic"),
        ("Traceback (most recent call last):", "panic"),
        # Go's deadlock message. Lands in `fatal` rather than `panic` because
        # "goroutines are asleep" has no id for the goroutine pattern to bite on.
        # Both are high-severity and surface together, so the distinction costs
        # nothing here -- recorded rather than "fixed", because widening the panic
        # pattern to bare `goroutine` would swallow every routine debug line.
        ("fatal error: all goroutines are asleep", "fatal"),
        ("Killed process 123 (java) out of memory", "oom"),
        ("Container was OOMKilled", "oom"),
        ("write /data: no space left on device", "disk"),
        ("x509: certificate has expired", "cert"),
        ("permission denied while opening /etc/shadow", "auth"),
        ("dial tcp 10.0.0.1:5432: connect: connection refused", "connection"),
        ("context deadline exceeded", "connection"),
        ("WARNING: option --foo is deprecated and will be removed in 2.0", "deprecation"),
        ("failed to load config", "error"),
        ("everything is fine", None),
        ("", None),
    ],
)
def test_classification(line, expected):
    assert classify(line)[0] == expected


def test_severity_orders_panic_above_generic_error():
    assert classify("panic: boom")[1] > classify("failed to load")[1]
    assert classify("out of memory")[1] > classify("deprecated")[1]


def test_detected_level_raises_the_floor_but_cannot_veto():
    """Loki's level is assigned by the emitting library, and libraries log panics
    at the wrong level all the time. Prose that says 'panic' is a panic whatever
    the level field claims."""
    assert classify("panic: boom", {"detected_level": "info"})[0] == "panic"


def test_detected_level_is_used_when_the_prose_gives_nothing():
    assert classify("something happened", {"detected_level": "error"})[0] == "error"
    assert classify("something happened", {"detected_level": "info"})[0] is None


def test_the_classifier_is_a_heuristic_and_we_know_it():
    """Honesty test. 'no errors found' contains 'error' and WILL be flagged. That
    is a documented limitation, not a bug — which is exactly why every finding
    carries its raw line so a reader can overrule it. If this ever starts
    returning None, the classifier grew a semantic model and this test should be
    replaced, not deleted."""
    assert classify("no errors found")[0] == "error"


# ---- clustering ------------------------------------------------------

def _rec(line, ts=1, host="a", unit="x.service", level=None):
    labels = {"hostname": host, "systemd_unit": unit}
    if level:
        labels["detected_level"] = level
    return {"line": line, "timestamp": ts, "time": f"t{ts}", "labels": labels}


def test_clusters_count_repeats():
    records = [_rec("connection to 10.0.0.1 failed"), _rec("connection to 10.0.0.2 failed")]
    out = cluster(records)
    assert len(out) == 1
    assert out[0]["count"] == 2


def test_clusters_track_how_many_sources_are_affected():
    """'This error is on one host' and 'this error is on all twenty-one' are
    completely different problems that look identical in a flat log tail."""
    records = [_rec("boom failed", host=f"h{i}", unit=f"u{i}.service") for i in range(3)]
    out = cluster(records)
    assert out[0]["sourceCount"] == 3
    assert len(out[0]["sources"]) == 3


def test_severity_beats_volume_in_the_ranking():
    """A single panic outranks a thousand timeouts, because the panic is the thing
    you have to go and fix."""
    records = [_rec("connection refused", ts=i) for i in range(50)] + [_rec("panic: boom", ts=99)]
    out = cluster(records)
    assert out[0]["category"] == "panic"
    assert out[0]["count"] == 1
    assert out[1]["count"] == 50


def test_cluster_tracks_the_window_it_was_seen_in():
    records = [_rec("boom failed", ts=10), _rec("boom failed", ts=5), _rec("boom failed", ts=20)]
    out = cluster(records)
    assert out[0]["firstSeen"] == "t5"
    assert out[0]["lastSeen"] == "t20"


def test_top_caps_the_output():
    records = [_rec(f"distinct problem {chr(97 + i)} failed") for i in range(20)]
    assert len(cluster(records, top=5)) == 5


def test_examples_are_capped_but_present():
    """Each finding must carry a raw line — a fingerprint alone is not something a
    human can act on, and the classifier is a heuristic they need to check."""
    records = [_rec("connection to 10.0.0.1 failed") for _ in range(10)]
    out = cluster(records, examples=1)
    assert out[0]["examples"] == ["connection to 10.0.0.1 failed"]


def test_internal_bookkeeping_does_not_leak_into_the_payload():
    out = cluster([_rec("boom failed")])
    assert not [k for k in out[0] if k.startswith("_")], "private keys must not reach stdout"


def test_clustering_empty_input():
    assert cluster([]) == []


# ---- similar ---------------------------------------------------------

def test_to_regex_round_trips_a_fingerprint_into_a_search():
    """The 'has this happened elsewhere?' move: one line -> its shape -> a query
    that finds the same message with different ids."""
    import re

    fp = fingerprint("connection to 10.0.0.7:5432 failed after 1.2s")
    rx = to_regex(fp)
    assert re.search(rx, "connection to 10.0.0.9:5432 failed after 9.9s")
    assert not re.search(rx, "totally unrelated line")


def test_to_regex_escapes_literal_regex_metacharacters():
    """A log line is not a regex. `[warn] cache miss (hit rate 0.5)` would be a
    parse error or, worse, a wrong match."""
    import re

    fp = fingerprint("[warn] cache miss (hit rate 0.5)")
    rx = to_regex(fp)
    assert re.search(rx, "[warn] cache miss (hit rate 0.9)")


def test_to_regex_is_unanchored_on_purpose():
    """Log lines arrive with collector-dependent prefixes (a syslog header, a
    container tag); anchoring would match none of them."""
    import re

    rx = to_regex(fingerprint("cache miss"))
    assert re.search(rx, "2026-07-17 host docker[1]: cache miss")


# ---- format detection ------------------------------------------------

def test_detect_json():
    records = [{"line": '{"level":"error","msg":"boom"}'} for _ in range(5)]
    assert detect_format(records) == "json"


def test_detect_logfmt():
    records = [{"line": 'level=error msg="boom" ts=1'} for _ in range(5)]
    assert detect_format(records) == "logfmt"


def test_detect_text():
    records = [{"line": "just some prose about a thing"} for _ in range(5)]
    assert detect_format(records) == "text"


def test_one_equals_sign_is_not_logfmt():
    """Two pairs is the floor: prose containing 'x = 1' is not structured, and
    claiming it is would send the reader off writing `| logfmt` pipelines that
    silently match nothing."""
    assert detect_format([{"line": "the answer = 42"} for _ in range(5)]) == "text"


def test_detect_format_on_nothing():
    assert detect_format([]) == "unknown"


def test_looks_like_json():
    assert looks_like_json('{"a":1}') is True
    assert looks_like_json("not json") is False
    assert looks_like_json("") is False


# ---- the headline ----------------------------------------------------

def test_summarise_counts_by_category():
    records = [_rec("panic: boom"), _rec("connection refused"), _rec("connection reset")]
    clusters = cluster(records)
    s = summarise(records, clusters)
    assert s["linesAnalysed"] == 3
    assert s["distinctProblems"] == 3
    assert s["worst"]["category"] == "panic"


def test_verdict_on_a_clean_window():
    v = verdict([])
    assert v["healthy"] is True


def test_verdict_is_conservative_by_design():
    """`healthy` is false whenever ANYTHING matched. A tool that says 'looks fine'
    while a panic sits in its own output has actively misled someone; 'I found
    things, you judge' is the honest contract for a heuristic."""
    v = verdict(cluster([_rec("deprecated: use --bar instead")]))
    assert v["healthy"] is False
    assert "deprecation" in v["summary"]
