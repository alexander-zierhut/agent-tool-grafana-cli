"""The role matrix — what a Viewer/Editor/Admin token can actually do.

Needs the throwaway stack (`docker compose up -d && ./scripts/bootstrap_test_stack.sh`),
because it needs to mint all three roles, which you cannot do against somebody's
production Grafana.

**Why this file exists, specifically.** The README, the guide, the `auth login`
prompt and the 403 hint all used to say: *listing datasources needs the org Admin
role; an Editor can query but not enumerate.* That was **reasoned, never
measured, and wrong** — a Viewer enumerates datasources perfectly well. The claim
survived a spike, a code review and six agents, and it was talking users into
granting a CLI org-administration rights it has never once used. Over-privilege
by documentation is a security bug, not a typo.

One `docker compose up` disproved it in a minute. So the matrix lives here as
executable assertions rather than prose: if Grafana changes what a role grants,
this fails, instead of the docs quietly becoming wrong again.
"""

from __future__ import annotations

import pytest

from grafanacli.client import Client
from grafanacli.errors import AuthError

# `live_env` reads the environment snapshot taken BEFORE the autouse `_hermetic`
# fixture stripped it. Reading os.environ here would see nothing -- which is
# exactly how this file first "passed" by skipping everything.
from .conftest import live_env

pytestmark = pytest.mark.integration


def _token(var: str) -> str:
    value = live_env(var)
    if not value:
        pytest.skip(
            f"{var} is not set. These tests need the throwaway stack: "
            f"docker compose up -d && eval \"$(./scripts/bootstrap_test_stack.sh --export)\""
        )
    return value


@pytest.fixture
def url() -> str:
    return live_env("GRAFANA_URL") or pytest.skip("GRAFANA_URL is not set")


@pytest.fixture
def clients(url):
    """One client per role, all pointed at org 1 of the throwaway stack."""
    made = {
        role: Client(url, _token(var), user_agent="agent-tool-grafana-cli/test")
        for role, var in (
            ("viewer", "GRAFANA_TOKEN_VIEWER"),
            ("editor", "GRAFANA_TOKEN_EDITOR"),
            ("admin", "GRAFANA_TOKEN"),
        )
    }
    yield made
    for c in made.values():
        c.close()


def _writes_allowed() -> bool:
    """The safety interlock.

    Only `scripts/bootstrap_test_stack.sh` sets this, and it only ever talks to a
    throwaway compose stack. So pointing the suite at a real Grafana runs the
    read-only tier and physically cannot reach the write tier — the alternative,
    trusting everyone to remember which URL is in their shell, is not a control.
    """
    return live_env("GRAFANA_ALLOW_WRITES") == "1"


needs_writes = pytest.mark.skipif(
    not _writes_allowed(),
    reason="refusing to write: GRAFANA_ALLOW_WRITES=1 is set only by the throwaway test stack",
)


# ---- reads: every role can do all of it -----------------------------

@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
def test_every_role_can_enumerate_datasources(clients, role):
    """THE correction. `logs sources` — the tool's headline feature — works on a
    **Viewer** token. The old claim that this needed Admin was false."""
    datasources = clients[role].get("/datasources")
    assert isinstance(datasources, list)
    assert any(d["type"] == "loki" for d in datasources), f"{role} sees no Loki"


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
def test_every_role_can_query_logs_through_the_proxy(clients, role):
    uid = live_env("GRAFANA_TEST_LOKI_UID") or pytest.skip("no seeded Loki uid")
    payload = clients[role].ds_proxy(uid, "loki/api/v1/labels")
    assert payload.get("status") == "success"


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
@pytest.mark.parametrize(
    "path",
    [
        "/v1/provisioning/alert-rules",
        "/v1/provisioning/contact-points",
        "/v1/provisioning/policies",
        "/alertmanager/grafana/config/api/v1/receivers",
    ],
)
def test_every_role_can_read_the_alerting_surface(clients, role, path):
    """So `grafana-cli alert route` and `grafana-cli notify check` — which only read — work on a
    Viewer. Knowing whether your alerts reach anyone should not require the power
    to change them."""
    assert clients[role].get(path) is not None


# ---- writes: Editor and up ------------------------------------------

@needs_writes
def test_viewer_cannot_create_a_dashboard(clients):
    with pytest.raises(AuthError):
        clients["viewer"].post(
            "/dashboards/db",
            json={"dashboard": {"title": "viewer-should-fail", "panels": []},
                  "folderUid": live_env("GRAFANA_TEST_FOLDER")},
        )


@needs_writes
@pytest.mark.parametrize("role", ["editor", "admin"])
def test_editor_and_admin_can_create_a_dashboard(clients, role):
    res = clients[role].post(
        "/dashboards/db",
        json={"dashboard": {"title": f"created-by-{role}", "panels": []},
              "folderUid": live_env("GRAFANA_TEST_FOLDER"), "overwrite": True},
    )
    assert res["uid"]
    clients["admin"].delete(f"/dashboards/uid/{res['uid']}")


@needs_writes
def test_viewer_cannot_create_a_contact_point(clients):
    """The answer to "what role do I need for notifications?": Editor, not Admin."""
    with pytest.raises(AuthError):
        clients["viewer"].post(
            "/v1/provisioning/contact-points",
            json={"name": "viewer-should-fail", "type": "webhook",
                  "settings": {"url": "http://localhost:1/x"}},
        )


@needs_writes
@pytest.mark.parametrize("role", ["editor", "admin"])
def test_editor_and_admin_can_manage_contact_points(clients, role):
    """Editor is sufficient for the whole notification surface. Admin adds
    nothing — which is the point: don't hand out Admin for this."""
    name = f"cp-{role}"
    clients[role].post(
        "/v1/provisioning/contact-points",
        json={"name": name, "type": "webhook", "settings": {"url": "http://localhost:1/x"}},
    )
    points = clients[role].get("/v1/provisioning/contact-points")
    made = next((p for p in points if p.get("name") == name), None)
    assert made, f"{role} created a contact point that does not list"
    clients["admin"].delete(f"/v1/provisioning/contact-points/{made['uid']}")


@needs_writes
def test_viewer_cannot_create_an_alert_rule(clients):
    with pytest.raises(AuthError):
        clients["viewer"].post("/v1/provisioning/alert-rules", json=_rule("viewer-should-fail"))


@needs_writes
@pytest.mark.parametrize("role", ["editor", "admin"])
def test_editor_and_admin_can_create_an_alert_rule(clients, role):
    created = clients[role].post("/v1/provisioning/alert-rules", json=_rule(f"rule-by-{role}"))
    assert created["uid"]
    clients["admin"].delete(f"/v1/provisioning/alert-rules/{created['uid']}")


@needs_writes
def test_editor_can_pause_and_unpause_a_rule(clients):
    """Backs the "VERIFIED" claim in `alert pause`/`unpause`. Those docstrings
    used to say the PUT shape was an unverified guess (built read-only against
    production); that caveat is gone, so a test has to make it true.

    Proves the read-modify-write specifically: PUT the WHOLE rule back with
    `isPaused` flipped. A partial body would drop every field it omits, because
    the provisioning API replaces the rule wholesale rather than patching it.
    """
    client = clients["editor"]
    created = client.post("/v1/provisioning/alert-rules", json=_rule("pause-me"))
    uid = created["uid"]
    try:
        assert created.get("isPaused") is False

        rule = client.get(f"/v1/provisioning/alert-rules/{uid}")
        rule["isPaused"] = True
        paused = client.put(f"/v1/provisioning/alert-rules/{uid}", json=rule)
        assert paused["isPaused"] is True
        # The rest of the rule must survive the round-trip -- the whole reason we
        # read-modify-write instead of sending {"isPaused": true}.
        assert paused["title"] == "pause-me"
        assert paused["condition"] == "A"

        rule["isPaused"] = False
        assert client.put(f"/v1/provisioning/alert-rules/{uid}", json=rule)["isPaused"] is False
    finally:
        clients["admin"].delete(f"/v1/provisioning/alert-rules/{uid}")


def _rule(title: str) -> dict:
    """A minimal valid Grafana alert rule.

    Pure `__expr__` math, no datasource: the point here is the PERMISSION, and a
    rule that also had to query Loki would fail for reasons that have nothing to
    do with the role under test.
    """
    return {
        "title": title,
        "folderUID": live_env("GRAFANA_TEST_FOLDER"),
        "ruleGroup": "roles",
        "orgID": 1,
        "for": "5m",
        "condition": "A",
        "noDataState": "NoData",
        "execErrState": "Error",
        "data": [
            {
                "refId": "A",
                "datasourceUid": "__expr__",
                "relativeTimeRange": {"from": 600, "to": 0},
                "model": {"type": "math", "expression": "1 > 0", "refId": "A"},
            }
        ],
    }


# ---- what the CLI reports about all this ----------------------------

def test_doctor_reports_capability_honestly_for_a_viewer(clients):
    """`server doctor` must not confuse "cannot enumerate" with "cannot write".
    A Viewer's report should show discovery working and writes not."""
    perms = clients["viewer"].get("/access-control/user/permissions")
    assert "datasources:read" in perms, "a Viewer CAN discover"
    assert "alert.notifications.receivers:write" not in perms, "a Viewer cannot write notifications"


def test_editor_has_everything_this_cli_needs(clients):
    """The recommendation the docs now make, asserted rather than believed."""
    perms = clients["editor"].get("/access-control/user/permissions")
    for permission in (
        "datasources:read",
        "datasources:query",
        "alert.rules:create",
        "alert.notifications.receivers:write",
        "dashboards:create",
    ):
        assert permission in perms, f"Editor is missing {permission}, which this CLI uses"


def test_admin_grants_nothing_extra_that_this_cli_uses(clients):
    """Guards the advice "don't use Admin". If Admin ever gains a permission this
    CLI needs, this fails and the recommendation has to change."""
    editor = set(clients["editor"].get("/access-control/user/permissions"))
    admin = set(clients["admin"].get("/access-control/user/permissions"))
    used_by_this_cli = {
        "datasources:read", "datasources:query", "datasources:explore",
        "dashboards:create", "dashboards:read", "dashboards:write", "dashboards:delete",
        "folders:read",
        "alert.rules:create", "alert.rules:read", "alert.rules:write", "alert.rules:delete",
        "alert.instances:read",
        "alert.notifications.receivers:read", "alert.notifications.receivers:write",
        "alert.notifications.time-intervals:read",
        "annotations:read",
    }
    # NOT listed, deliberately: `org.users:read` IS an Admin-only permission that
    # Editor lacks — the first version of this set named it and the test failed,
    # correctly. But no command in this CLI calls /api/org/users (grep it), so it
    # is not a reason to recommend Admin. The set means "permissions this tool
    # actually exercises"; padding it with plausible-looking ones would
    # re-manufacture the very over-privilege advice this file exists to refute.
    extra = (admin - editor) & used_by_this_cli
    assert not extra, (
        f"Admin grants {sorted(extra)} that Editor lacks and this CLI uses — the "
        f"docs recommend Editor and would now be wrong."
    )


# ---- the interlock itself -------------------------------------------

def test_the_write_interlock_is_off_by_default_everywhere_but_the_test_stack():
    """A meta-test, and it earns its place: the destructive tests above are
    guarded by an env var, and an interlock nobody checks is decoration. If
    `bootstrap_test_stack.sh` stops exporting it, every write test silently skips
    and the suite goes green having tested nothing.
    """
    if live_env("GRAFANA_URL").startswith(("http://localhost", "http://127.0.0.1")):
        assert _writes_allowed(), (
            "pointed at the local test stack but GRAFANA_ALLOW_WRITES is unset — "
            "the write tests are silently skipping. Run bootstrap_test_stack.sh."
        )
    else:
        assert not _writes_allowed(), (
            "GRAFANA_ALLOW_WRITES is set while pointed at a NON-LOCAL Grafana. "
            "Refusing: these tests create and delete real objects."
        )
