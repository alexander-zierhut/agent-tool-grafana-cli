"""Live, READ-ONLY tests against a real Grafana.

Skipped unless `GRAFANA_URL` + `GRAFANA_TOKEN` are set (see conftest). The
multi-org tests additionally need `GRAFANA_TOKEN_ORG6` — a token from a
**different** org.

**Read-only, and that is a hard rule.** These run against a production instance.
Nothing here creates, updates or deletes anything: no dashboards, no alert rules,
no test notifications. A test suite that mutates production is a test suite people
stop running, and then it protects nothing. Write paths are covered by `--dry-run`
in the hermetic suite, which asserts the exact request without sending it.

What these are FOR: catching the day Grafana changes a shape we depend on. The
hermetic tests encode what we believe; these check the belief is still true.
"""

from __future__ import annotations

import pytest

from grafanacli import loki, routing, sources
from grafanacli.errors import DatasourceUnreachable, OpError, OrgMismatch
from grafanacli.timerange import TimeRange

pytestmark = pytest.mark.integration


# ---- reachability & identity ----------------------------------------

def test_health_is_unauthenticated_and_carries_the_version(live):
    """The first rung of `server doctor`: it separates 'the URL is wrong / the
    server is down' from 'your token is bad' before auth is involved at all."""
    from grafanacli.client import Client

    client = Client(live["url"], "not-a-real-token")
    try:
        health = client.health()
    finally:
        client.close()
    assert health["database"] == "ok"
    assert health["version"], "health must report a version"


def test_service_account_identity_shape(live_client):
    """Guards two facts that make a natural capability check WRONG:
    `id` is 0 and `isGrafanaAdmin` is false even for a token that is an org
    Admin. Never gate a feature on either."""
    user = live_client.get("/user")
    assert user["id"] == 0, "service accounts report id 0 -- do not key anything on it"
    assert user["isGrafanaAdmin"] is False, "server-admin != org-admin"
    assert str(user["uid"]).startswith("service-account:")
    assert user["orgId"] >= 1


def test_permission_names_are_not_capability(live_client):
    """The scope matters. Our token HAS `orgs:read`, scoped to nothing useful, and
    `/api/orgs` still 403s. A doctor that reports permission names without scopes
    would confidently claim a capability the token does not have."""
    perms = live_client.get("/access-control/user/permissions")
    assert "orgs:read" in perms

    with pytest.raises(OpError):
        live_client.get("/orgs")


def test_the_capability_that_actually_gates_discovery(live_client):
    """`datasources:read` with a real scope is what makes `logs sources` possible.
    If this ever comes back unscoped, discovery breaks and doctor must say why."""
    perms = live_client.get("/access-control/user/permissions")
    assert "datasources:*" in perms.get("datasources:read", [])


# ---- THE multi-org contract -----------------------------------------

def test_tokens_are_hard_scoped_to_one_org(live_org2):
    """The finding the whole multi-org design rests on, tested in BOTH directions
    so a one-sided quirk cannot masquerade as a rule.

    If this ever passes cross-org, `graf` could switch orgs on a flag and the
    profile-per-org model would be unnecessary ceremony. It does not pass."""
    from grafanacli.client import Client

    org1 = Client(live_org2["url"], live_org2["token"])
    org2 = Client(live_org2["url"], live_org2["token_org2"])
    try:
        home1 = org1.get("/org")["id"]
        home2 = org2.get("/org")["id"]
        assert home1 != home2, "the two tokens must be from different orgs for this to prove anything"

        # Each token, asserting the OTHER's org -> hard 401, both ways.
        for client, foreign in ((org1, home2), (org2, home1)):
            client.org_id = foreign
            client._client.headers["X-Grafana-Org-Id"] = str(foreign)
            with pytest.raises(OrgMismatch) as e:
                client.get("/org")
            assert e.value.exit_code == 9
            assert "one profile per org" in str(e.value)
    finally:
        org1.close()
        org2.close()


def test_asserting_your_own_org_is_harmless(live_client):
    """The other half: the header must be safe to send always. If asserting the
    correct org failed, the client could not send it by default and we would lose
    the wrong-org guard entirely."""
    org_id = live_client.get("/org")["id"]
    live_client.org_id = org_id
    live_client._client.headers["X-Grafana-Org-Id"] = str(org_id)
    assert live_client.get("/org")["id"] == org_id


def test_datasource_uids_differ_per_org(live_org2):
    """Why the sticky context is keyed by profile. The same logical Loki has a
    different uid in each org, so a global default would resolve to a uid that
    does not exist and Grafana would blame the datasource."""
    from grafanacli.client import Client

    org1 = Client(live_org2["url"], live_org2["token"])
    org2 = Client(live_org2["url"], live_org2["token_org2"])
    try:
        uids1 = {d["uid"] for d in sources.list_datasources(org1)}
        uids2 = {d["uid"] for d in sources.list_datasources(org2)}
        assert uids1 and uids2
        assert not (uids1 & uids2), "no datasource uid may be shared across orgs"
    finally:
        org1.close()
        org2.close()


def test_user_orgs_endpoint_is_useless_for_service_accounts(live_client):
    """Documents a live oddity so nobody 'fixes' org listing by reaching for it:
    `/api/user/orgs` answers **304 with an empty body** for a service account.
    A client expecting JSON gets None."""
    assert live_client.get("/user/orgs") is None


# ---- discovery: the killer feature ----------------------------------

def test_survey_finds_log_datasources_and_counts_their_labels(live_client):
    window = TimeRange.resolve(since="1h")
    report = sources.survey(live_client, window)

    assert report["window"]["seconds"] == 3600
    assert report["datasources"], "the live instance must have at least one log datasource"

    reachable = [d for d in report["datasources"] if d.get("reachable")]
    assert reachable, "at least one Loki must answer"
    ds = reachable[0]
    assert ds["type"] == "loki"
    assert ds["labelCount"] > 0
    assert all({"label", "values", "useful", "sample"} <= set(r) for r in ds["labels"])


def test_survey_reports_a_dead_backend_without_blanking_the_report(live_client):
    """The live instance has a datasource whose backend is down (502, empty body).
    That must show up as a per-row error beside the healthy ones -- one dead
    backend must never take the whole answer with it."""
    window = TimeRange.resolve(since="1h")
    report = sources.survey(live_client, window)
    for entry in report["datasources"]:
        assert "uid" in entry and "name" in entry
        assert entry.get("reachable") is not False or entry.get("error"), (
            "an unreachable datasource must carry an explanation"
        )


def test_label_cardinality_is_the_useful_signal(live_client):
    """Asserts the SHAPE of the answer, not the values -- the real hostnames and
    unit names are infrastructure and stay out of this repo."""
    window = TimeRange.resolve(since="1h")
    ds = next((d for d in sources.log_datasources(live_client) if d["logs"] == "supported"), None)
    if ds is None:
        pytest.skip("no queryable log datasource")
    try:
        report = sources.describe_loki(live_client, str(ds["uid"]), window)
    except DatasourceUnreachable:
        pytest.skip("this datasource's backend is down right now")

    labels = report["labels"]
    assert labels, "Loki must expose at least one label"
    assert labels == sorted(labels, key=lambda r: (-r["values"], r["label"])), "ranked by cardinality"
    assert any(r["useful"] for r in labels), "at least one label must discriminate"


def test_labels_are_time_bounded(live_client):
    """Not a detail: it means 'what can I get logs from' has a different answer at
    09:00 than at 17:00, which is why every payload carries its window."""
    ds = next((d for d in sources.log_datasources(live_client) if d["logs"] == "supported"), None)
    if ds is None:
        pytest.skip("no queryable log datasource")
    try:
        recent = set(sources.loki_labels(live_client, str(ds["uid"]), TimeRange.resolve(since="1h")))
        wide = set(sources.loki_labels(live_client, str(ds["uid"]), TimeRange.resolve(since="30d")))
    except DatasourceUnreachable:
        pytest.skip("this datasource's backend is down right now")
    assert recent <= wide, "a wider window can only ever reveal more labels, never fewer"


# ---- querying --------------------------------------------------------

def _first_loki(client):
    ds = next((d for d in sources.log_datasources(client) if d["logs"] == "supported"), None)
    if ds is None:
        pytest.skip("no queryable log datasource")
    return ds


@pytest.mark.needs_loki
def test_a_real_query_returns_parseable_lines(live_client):
    window = TimeRange.resolve(since="1h")
    ds = _first_loki(live_client)
    try:
        labels = {r["label"]: r["values"] for r in
                  sources.describe_loki(live_client, str(ds["uid"]), window)["labels"]}
    except DatasourceUnreachable:
        pytest.skip("this datasource's backend is down right now")

    match_all = loki.pick_match_all_label({k: [""] * v for k, v in labels.items()})
    query = loki.build_query({}, match_all_label=match_all)
    start, end = window.loki()
    payload = live_client.ds_proxy(
        str(ds["uid"]), "loki/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "limit": 5, "direction": "backward"},
    )
    records = loki.parse_streams(payload)
    if not records:
        pytest.skip("no logs in the last hour on this instance")
    assert all({"timestamp", "time", "line", "labels"} == set(r) for r in records)
    assert records == sorted(records, key=lambda r: r["timestamp"], reverse=True)


@pytest.mark.needs_loki
def test_loki_reads_timestamps_by_DIGIT_COUNT_not_unit(live_client):
    """THE trap, and it is not the one the docs imply.

    Loki's `parseTimestamp` switches on the **length of the string**, not on any
    declared unit: `len(value) <= 10` -> seconds, otherwise nanoseconds. So

      * 10-digit **seconds**     work fine,
      * 19-digit **nanoseconds** work fine,
      * 13-digit **milliseconds** are read as NANOSECONDS -> a window in 1970 ->
        `{"status":"success"}` with an empty result, no error, no warning.

    Milliseconds are the dangerous case precisely because they are the natural
    thing to reach for: `Date.now()`, `time.time()*1000` and Grafana's own UI all
    speak millis. An empty result is indistinguishable from "there are no logs",
    which is the worst failure available to a tool whose job is answering "what
    happened".

    This test earns its keep twice over: it was written asserting the OPPOSITE
    (that seconds fail), and the live server contradicted it. The spike note it
    was based on was wrong. Read the server, not the notes.
    """
    window = TimeRange.resolve(since="1h")
    ds = _first_loki(live_client)
    try:
        labels = sources.loki_labels(live_client, str(ds["uid"]), window)
    except DatasourceUnreachable:
        pytest.skip("this datasource's backend is down right now")
    if not labels:
        pytest.skip("no labels to build a query from")

    query = loki.build_query({}, match_all_label=labels[0])
    ns_start, ns_end = window.loki()
    s_start, s_end = (int(v) for v in window.prometheus())

    def ask(start, end):
        return loki.parse_streams(live_client.ds_proxy(
            str(ds["uid"]), "loki/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "limit": 5, "direction": "backward"},
        ))

    if not ask(ns_start, ns_end):
        pytest.skip("no logs in the last hour to demonstrate with")

    # 10 digits: read as seconds, works.
    assert len(str(s_start)) == 10
    assert ask(s_start, s_end), "10-digit seconds must work -- Loki reads <=10 digits as seconds"

    # 13 digits: read as NANOSECONDS -> 1970 -> silently empty.
    ms_start, ms_end = s_start * 1000, s_end * 1000
    assert len(str(ms_start)) == 13
    payload = live_client.ds_proxy(
        str(ds["uid"]), "loki/api/v1/query_range",
        params={"query": query, "start": ms_start, "end": ms_end, "limit": 5, "direction": "backward"},
    )
    assert payload.get("status") == "success", "millis do not even error -- that is the trap"
    assert loki.parse_streams(payload) == [], "millis are read as nanos, land in 1970, return nothing"


@pytest.mark.needs_loki
def test_we_always_send_the_unambiguous_encoding(live_client):
    """Whatever Loki's parsing quirks, `window.loki()` must emit 19 digits — the
    encoding that cannot be misread as anything else."""
    ns_start, ns_end = TimeRange.resolve(since="1h").loki()
    assert len(str(ns_start)) == 19
    assert len(str(ns_end)) == 19


@pytest.mark.needs_loki
def test_detected_level_filters_but_is_not_an_indexed_label(live_client):
    """Both halves of the derived-label trap, live:
      1. `detected_level` is absent from /labels, yet
      2. it filters correctly as a pipeline stage.
    This is why `build_query` puts level in the pipeline and never the selector."""
    window = TimeRange.resolve(since="1h")
    ds = _first_loki(live_client)
    try:
        labels = sources.loki_labels(live_client, str(ds["uid"]), window)
    except DatasourceUnreachable:
        pytest.skip("this datasource's backend is down right now")
    if not labels:
        pytest.skip("no labels to build a query from")

    assert "detected_level" not in labels, "detected_level must NOT be an indexed label"

    query = loki.build_query({}, level="error", match_all_label=labels[0])
    start, end = window.loki()
    records = loki.parse_streams(live_client.ds_proxy(
        str(ds["uid"]), "loki/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "limit": 5, "direction": "backward"},
    ))
    if not records:
        pytest.skip("no error-level logs in the last hour")
    assert all(r["labels"].get("detected_level") == "error" for r in records)


# ---- alerting --------------------------------------------------------

def test_the_two_contact_point_endpoints_disagree(live_client):
    """Live proof that reading one endpoint is not enough: `provisioning/
    contact-points` lists contact points, the alertmanager config lists receivers,
    and a receiver with no integrations appears in the second and not the first.
    Read only the first and a black hole is invisible."""
    provisioned = live_client.get("/v1/provisioning/contact-points")
    receivers = live_client.get("/alertmanager/grafana/config/api/v1/receivers")
    assert isinstance(provisioned, list) and isinstance(receivers, list)

    hollow = [r for r in receivers if not routing.integration_names(r)]
    if not hollow:
        pytest.skip("this instance has no hollow receivers right now -- nothing to prove")
    names = {r["name"] for r in provisioned}
    assert any(r["name"] not in names for r in hollow), (
        "a receiver with no integrations should be missing from the provisioning list"
    )


def test_every_firing_alert_can_be_routed(live_client):
    """The end-to-end join, against real data: rule labels -> policy tree ->
    receiver -> integrations. It asserts the report is PRODUCIBLE, not that the
    instance is healthy — the instance may legitimately be misconfigured, and
    saying so is the feature."""
    tree = live_client.get("/v1/provisioning/policies")
    receivers = live_client.get("/alertmanager/grafana/config/api/v1/receivers")
    alerts = live_client.get("/alertmanager/grafana/api/v2/alerts")
    if not alerts:
        pytest.skip("nothing firing right now")

    for alert in alerts[:5]:
        report = routing.delivery_report(alert.get("labels") or {}, tree, receivers)
        assert isinstance(report["delivered"], bool)
        assert report["routes"], "every alert must route somewhere, even if only to the root"
        # Cross-check our walk against Grafana's own answer, which the v2 alerts
        # endpoint helpfully includes. If these ever disagree, OUR routing model
        # is wrong and `alert route` is lying -- there is no more direct test of
        # this feature's correctness available.
        theirs = {r.get("name") for r in alert.get("receivers") or []}
        ours = {r["receiver"] for r in report["routes"]}
        if theirs:
            assert ours == theirs, f"our routing disagrees with Grafana's: {ours} != {theirs}"


def test_alert_rules_and_their_folders_are_readable(live_client):
    rules = live_client.get("/v1/provisioning/alert-rules")
    assert isinstance(rules, list)
    for rule in rules[:5]:
        labels = routing.rule_labels(rule)
        assert labels.get("alertname"), "every rule must yield an alertname for routing"


def test_alert_rule_permissions_are_folder_scoped(live_client):
    """Creating a rule needs `alert.rules:create` in the TARGET folder, not
    globally. `alert create` must say which folders are writable rather than
    letting the user discover it via a 403."""
    perms = live_client.get("/access-control/user/permissions")
    scopes = perms.get("alert.rules:create")
    if scopes is None:
        pytest.skip("this token cannot create alert rules at all")
    assert any(str(s).startswith("folders:") for s in scopes), (
        "alert rule creation is scoped to folders"
    )


# ---- dashboards ------------------------------------------------------

def test_search_pagination_terminates(live_client):
    hits = list(live_client.search(params={"type": "dash-db"}, limit=10))
    assert isinstance(hits, list)
    for hit in hits:
        assert "uid" in hit and "title" in hit


def test_folders_include_a_virtual_one(live_client):
    """`/api/folders` returns `{"id": -1, "uid": "sharedwithme"}` — not a real
    folder. Offering it as a create target produces a confusing failure."""
    folders = live_client.get("/folders")
    assert isinstance(folders, list)
    virtual = [f for f in folders if f.get("id", 0) < 0]
    for f in virtual:
        assert f["uid"] == "sharedwithme"
