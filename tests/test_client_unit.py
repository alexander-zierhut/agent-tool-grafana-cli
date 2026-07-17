"""HTTP client behaviour — hermetic, no network.

Error *mapping* is tested by handing `_raise_for_error` a real `httpx.Response`,
which is honest about what it proves: given this exact response, do we raise the
right thing with the right words. The bodies below are copied verbatim from the
live instance (see `spike/VERIFIED_FINDINGS.md`), so these tests fail if Grafana
changes its shapes — which is precisely when we want to hear about it.
"""

from __future__ import annotations

import httpx
import pytest

from grafanacli.client import Client, _permission_hint, _uid_from_proxy_url
from grafanacli.errors import (
    ApiError,
    AuthError,
    ConflictError,
    DatasourceUnreachable,
    DryRun,
    NotFoundError,
    OrgMismatch,
    ValidationError,
)


def _resp(status: int, json=None, text: str | None = None) -> httpx.Response:
    kw = {"json": json} if json is not None else {"text": text or ""}
    return httpx.Response(status, request=httpx.Request("GET", "https://g.example.com/api/x"), **kw)


# ---- the org header --------------------------------------------------

def test_org_header_is_sent_when_the_profile_knows_its_org():
    h = Client._headers("glsa_tok", 6, "ua")
    assert h["X-Grafana-Org-Id"] == "6"
    assert h["Authorization"] == "Bearer glsa_tok"


def test_org_header_is_omitted_when_unknown():
    """Sending a guessed org would be worse than sending none: a wrong guess is a
    hard 401 on a token that would otherwise have worked fine."""
    assert "X-Grafana-Org-Id" not in Client._headers("glsa_tok", None, "ua")


def test_org_header_is_an_assertion_not_a_switch():
    """Documents the semantics the header actually has, verified live in both
    directions: a service-account token CANNOT change org with it. What it buys is
    a loud failure instead of silently querying the wrong org."""
    with pytest.raises(OrgMismatch) as e:
        Client._raise_for_error(
            _resp(401, {
                "message": "API key does not belong to the requested organization",
                "messageId": "api-key.organization-mismatch",
            })
        )
    assert "one profile per org" in str(e.value)
    assert e.value.exit_code == 9


def test_org_mismatch_is_not_an_autherror():
    """The distinction that earns the separate code: an agent catching AuthError
    would re-run `auth login`, and re-authenticating with the same token fixes
    nothing. Different problem, different fix, different code."""
    assert not issubclass(OrgMismatch, AuthError)


def test_a_plain_401_is_still_an_autherror():
    with pytest.raises(AuthError) as e:
        Client._raise_for_error(_resp(401, {"message": "invalid token"}))
    assert e.value.exit_code == 4


# ---- the permission hint --------------------------------------------

def test_403_names_the_permission_and_what_it_costs_you():
    """Grafana buries the only useful part -- the permission name -- at the end of
    a sentence that says nothing else. Live body, copied verbatim."""
    with pytest.raises(AuthError) as e:
        Client._raise_for_error(_resp(403, {
            "accessErrorId": "ACE3392348646",
            "message": "You'll need additional permissions to perform this action. Permissions needed: datasources:read",
            "title": "Access denied",
        }))
    msg = str(e.value)
    assert "datasources:read" in msg
    assert "Admin" in msg, "must say which role fixes it"
    assert "logs sources" in msg, "must say what it costs you"


def test_permission_hint_generalises_to_permissions_we_have_not_met():
    hint = _permission_hint(
        "You'll need additional permissions to perform this action. "
        "Permissions needed: alert.notifications.config-history:read"
    )
    assert hint == "your token lacks the alert.notifications.config-history:read permission."


def test_permission_hint_ignores_unrelated_prose():
    assert _permission_hint("something else entirely") is None
    assert _permission_hint(None) is None
    assert _permission_hint({"not": "a string"}) is None


# ---- the datasource tunnel ------------------------------------------

def test_proxy_detection():
    assert Client._is_proxy("https://g.example.com/api/datasources/proxy/uid/abc/loki/api/v1/labels")
    assert not Client._is_proxy("https://g.example.com/api/datasources")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://g.example.com/api/datasources/proxy/uid/abc123/loki/api/v1/labels", "abc123"),
        ("https://g.example.com/api/datasources/proxy/uid/abc123?x=1", "abc123"),
        ("https://g.example.com/api/datasources", None),
    ],
)
def test_uid_is_recoverable_from_a_proxy_url_for_error_messages(url, expected):
    assert _uid_from_proxy_url(url) == expected


def test_502_through_the_proxy_is_a_datasource_problem_not_an_api_one():
    """The live 502: `content-length: 0`, so there is nothing to quote back. My
    own probe died in json.loads on exactly this, which is how it got documented."""
    resp = httpx.Response(
        502,
        request=httpx.Request("GET", "https://g.example.com/api/datasources/proxy/uid/ae9zq33kx01mbd/loki/api/v1/labels"),
        text="",
    )
    with pytest.raises(DatasourceUnreachable) as e:
        Client._raise_datasource_unreachable(resp, str(resp.request.url))
    msg = str(e.value)
    assert "ae9zq33kx01mbd" in msg
    assert "Grafana itself is fine" in msg, "must not send people to check their token"
    assert e.value.exit_code == 8


def test_datasource_unreachable_is_not_an_apierror():
    """Sibling, not subclass: an `except ApiError` ladder that swallowed this
    would report 'the API failed' and hide which of the two systems actually did."""
    assert not issubclass(DatasourceUnreachable, ApiError)


def test_a_404_inside_the_tunnel_blames_the_query_not_the_datasource():
    """Two very different 404s share a status code: 'no such datasource' and 'the
    datasource says no such path'. Conflating them sends you looking for a
    datasource that exists."""
    with pytest.raises(NotFoundError) as e:
        Client._raise_for_error(
            _resp(404, {"message": "not found"}),
            proxied=True,
            url="https://g.example.com/api/datasources/proxy/uid/abc/loki/api/v1/typo",
        )
    assert "answered 404 for that query path" in str(e.value)


def test_a_404_outside_the_tunnel_is_a_plain_not_found():
    with pytest.raises(NotFoundError) as e:
        Client._raise_for_error(_resp(404, {"message": "Dashboard not found"}))
    assert str(e.value) == "Dashboard not found"


# ---- the rest of the mapping ----------------------------------------

@pytest.mark.parametrize("status", [409, 412])
def test_conflict_statuses(status):
    """412 is Grafana's optimistic-locking status for dashboard saves; 409 shows
    up for a duplicate uid. Both mean 'someone else moved first'."""
    with pytest.raises(ConflictError):
        Client._raise_for_error(_resp(status, {"message": "version mismatch"}))


@pytest.mark.parametrize("status", [400, 422])
def test_validation_statuses(status):
    with pytest.raises(ValidationError):
        Client._raise_for_error(_resp(status, {"message": "bad request"}))


def test_unmapped_5xx_is_an_apierror_carrying_its_status():
    with pytest.raises(ApiError) as e:
        Client._raise_for_error(_resp(500, {"message": "boom"}))
    assert e.value.status == 500


def test_a_non_json_error_body_does_not_explode():
    """The proxy tunnels somebody else's server; an HTML error page is a real
    thing that arrives."""
    with pytest.raises(ApiError) as e:
        Client._raise_for_error(_resp(500, text="<html>gateway error</html>"))
    assert "gateway error" in str(e.value)


def test_an_empty_error_body_still_produces_a_message():
    with pytest.raises(ApiError) as e:
        Client._raise_for_error(_resp(503, text=""))
    assert "HTTP 503" in str(e.value)


# ---- transport behaviour --------------------------------------------

def _client_with(handler, **kw) -> Client:
    c = Client("https://g.example.com", "glsa_tok", **kw)
    c._client = httpx.Client(transport=httpx.MockTransport(handler), headers=c._headers("glsa_tok", kw.get("org_id"), "ua"))
    return c


def test_proxy_5xx_fails_fast_without_retrying():
    """Grafana ANSWERED -- so the network to Grafana is fine and the 502 is a fact
    about the backend, not a blip. `logs sources` and `scan` fan out across every
    datasource, so 4 attempts x N dead backends turns a clear report into a
    minute-long stall."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(502, text="")

    client = _client_with(handler)
    with pytest.raises(DatasourceUnreachable):
        client.ds_proxy("abc", "loki/api/v1/labels")
    assert len(calls) == 1, f"expected exactly one attempt, made {len(calls)}"


def test_a_non_proxy_502_is_still_retried():
    """A 502 in front of Grafana (a load balancer, a CDN) genuinely can be
    transient. The fail-fast rule is scoped to the tunnel on purpose."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(502, json={"message": "bad gateway"})

    client = _client_with(handler)
    client._backoff = staticmethod(lambda *a: None)  # do not actually sleep in a unit test
    with pytest.raises(ApiError):
        client.get("/datasources")
    assert len(calls) > 1, "a non-proxy 502 must be retried"


def test_dry_run_intercepts_writes_in_the_transport():
    """One chokepoint, so every write command -- present and future -- gets
    --dry-run for free and none can bypass it."""
    client = _client_with(lambda r: httpx.Response(200, json={}), dry_run=True)
    with pytest.raises(DryRun) as e:
        client.post("/dashboards/db", json={"dashboard": {"title": "x"}})
    assert e.value.request["method"] == "POST"
    assert e.value.request["body"] == {"dashboard": {"title": "x"}}


def test_dry_run_lets_reads_through():
    """Resolving a datasource name to a uid must really happen, or the printed
    request would be a guess."""
    client = _client_with(lambda r: httpx.Response(200, json={"ok": True}), dry_run=True)
    assert client.get("/datasources") == {"ok": True}


def test_dry_run_records_the_org_it_would_have_asserted():
    client = _client_with(lambda r: httpx.Response(200, json={}), dry_run=True, org_id=6)
    with pytest.raises(DryRun) as e:
        client.delete("/v1/provisioning/alert-rules/abc")
    assert e.value.request["org"] == 6


def test_none_params_are_dropped_not_sent_as_the_string_none():
    seen = {}

    def handler(request):
        seen["query"] = str(request.url.query.decode())
        return httpx.Response(200, json={})

    client = _client_with(handler)
    client.get("/search", params={"query": "x", "folder": None})
    assert "folder" not in seen["query"]
    assert "query=x" in seen["query"]


def test_an_empty_200_body_is_none_not_a_crash():
    client = _client_with(lambda r: httpx.Response(204, text=""))
    assert client.get("/x") is None


# ---- search pagination ----------------------------------------------

def test_search_stops_on_a_short_page():
    """Grafana's search returns a bare array with no total, so a short page IS the
    end -- the same rule as Drone's, and the OPPOSITE of OpenProject's, whose
    server caps page size and would loop forever under this rule. This is exactly
    why the transport is not shared between the tools."""
    pages = {1: [{"uid": str(i)} for i in range(1000)], 2: [{"uid": "last"}]}
    calls = []

    def handler(request):
        page = int(dict(request.url.params).get("page", 1))
        calls.append(page)
        return httpx.Response(200, json=pages.get(page, []))

    client = _client_with(handler)
    got = list(client.search())
    assert len(got) == 1001
    assert calls == [1, 2], "must stop after the short page, not probe a third"


def test_search_honours_limit_without_over_fetching():
    def handler(request):
        limit = int(dict(request.url.params).get("limit", 1000))
        return httpx.Response(200, json=[{"uid": str(i)} for i in range(limit)])

    client = _client_with(handler)
    assert len(list(client.search(limit=5))) == 5


def test_search_on_an_empty_result():
    client = _client_with(lambda r: httpx.Response(200, json=[]))
    assert list(client.search()) == []
