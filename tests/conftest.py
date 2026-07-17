"""Shared fixtures.

Two rules this file exists to enforce:

1. **Every test is hermetic by default.** `_hermetic` is autouse: it points the
   config dir at a tmp_path and strips every environment variable that could
   reach a real Grafana. A test suite that behaves differently on the maintainer's
   laptop than in CI is not a test suite, and the way that happens is an env var
   nobody remembered was exported.
2. **Live tests skip, never fail, when there is nothing to talk to.** The
   contributor promise is that `pip install -e '.[test]' && pytest` is green on a
   clean checkout with no Docker, no server and no token. A missing token is not
   a failure; it is the normal case for everyone but us.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: Every variable that could steer the CLI at a real server or a real token.
#: Listed exhaustively rather than prefix-matched: `GRAFANA_URL` does not share a
#: prefix with `GRAFANACLI_URL`, and a prefix sweep would miss exactly the
#: ecosystem variables most likely to be exported on a real machine.
_LEAKY_VARS = (
    "GRAFANA_URL",
    "GRAFANA_TOKEN",
    "GRAFANA_ORG_ID",
    "GRAFANACLI_URL",
    "GRAFANACLI_TOKEN",
    "GRAFANACLI_ORG_ID",
    "GRAFANACLI_PROFILE",
    "GRAFANACLI_FORMAT",
    "GRAFANACLI_CLI_FORMAT",
    "GRAFANACLI_CLI_FIELDS",
    "GRAFANACLI_DRY_RUN",
    "GRAFANACLI_STREAM",
    "GRAFANACLI_NO_CONTEXT",
)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """Isolate config + credentials, and cut every route to a real server."""
    monkeypatch.setenv("GRAFANACLI_CONFIG_DIR", str(tmp_path / "config"))
    for var in _LEAKY_VARS:
        monkeypatch.delenv(var, raising=False)
    # Never touch the developer's real keyring: it prompts on some desktops, and
    # a test that can block on a GUI unlock dialog will eventually hang CI.
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    yield


# ---- the live-instance gate -----------------------------------------


def _dotenv() -> dict[str, str]:
    """Read the gitignored .env, if the maintainer has one.

    Deliberately hand-rolled rather than python-dotenv: this must not add a
    dependency that a contributor is forced to install to run the hermetic suite.
    """
    path = Path(__file__).resolve().parent.parent / ".env"
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip("'\"")
    return out


def _live_config() -> dict[str, str]:
    """Live credentials from the environment, falling back to .env."""
    env = _dotenv()
    return {
        "url": os.environ.get("GRAFANA_URL") or env.get("GRAFANA_URL", ""),
        "token": os.environ.get("GRAFANA_TOKEN") or env.get("GRAFANA_TOKEN", ""),
        # The second org's token. Its absence skips only the multi-org tests,
        # never the rest -- most contributors will have one org at most.
        "token_org2": os.environ.get("GRAFANA_TOKEN_ORG6") or env.get("GRAFANA_TOKEN_ORG6", ""),
    }


@pytest.fixture(scope="session")
def live() -> dict:
    cfg = _live_config()
    if not cfg["url"] or not cfg["token"]:
        pytest.skip(
            "no live Grafana configured. Set GRAFANA_URL + GRAFANA_TOKEN (a "
            "service-account token with the Admin role) to run integration tests."
        )
    return cfg


@pytest.fixture(scope="session")
def live_org2(live) -> dict:
    if not live["token_org2"]:
        pytest.skip(
            "no second-org token. Set GRAFANA_TOKEN_ORG6 to a service-account "
            "token from a DIFFERENT org to run the multi-org tests."
        )
    return live


@pytest.fixture
def live_client(live, monkeypatch):
    """A Client pointed at the real instance.

    `_hermetic` has already stripped the env, so this fixture re-supplies exactly
    what it needs and nothing more — the live tests get a real server without
    reopening the door for the hermetic ones.
    """
    from grafanacli.client import Client

    client = Client(live["url"], live["token"], user_agent="agent-tool-grafana-cli/test")
    yield client
    client.close()


@pytest.fixture
def live_client_org2(live_org2):
    from grafanacli.client import Client

    client = Client(live_org2["url"], live_org2["token_org2"], user_agent="agent-tool-grafana-cli/test")
    yield client
    client.close()


# ---- the fake client ------------------------------------------------


class FakeClient:
    """A hand-rolled stand-in for `Client`.

    Hand-rolled rather than httpx's MockTransport, deliberately. A transport mock
    tempts you into simulating the *API*, and the API is wrong in ways you would
    encode wrongly — Loki's nanosecond timestamps, the two disagreeing contact
    point endpoints, a 502 with an empty body. Those live in the spike findings
    and the integration tests, where a real server can contradict us. Here we only
    need "given this response, does our code do the right thing", so a dict of
    canned responses is both sufficient and honest about what it proves.
    """

    def __init__(self, responses: dict | None = None, *, org_id: int | None = None):
        self.responses = responses or {}
        self.org_id = org_id
        self.calls: list[tuple[str, str, dict | None]] = []
        self.api_root = "https://grafana.example.com/api"
        self.web_root = "https://grafana.example.com"

    def _respond(self, method: str, path: str, params: dict | None = None):
        self.calls.append((method, path, params))
        for key, value in self.responses.items():
            if path == key or path.startswith(key.rstrip("*")) and key.endswith("*"):
                if isinstance(value, Exception):
                    raise value
                return value
        raise KeyError(f"FakeClient has no canned response for {method} {path}")

    def get(self, path, **kw):
        return self._respond("GET", path, kw.get("params"))

    def post(self, path, **kw):
        return self._respond("POST", path, kw.get("params"))

    def put(self, path, **kw):
        return self._respond("PUT", path, kw.get("params"))

    def patch(self, path, **kw):
        return self._respond("PATCH", path, kw.get("params"))

    def delete(self, path, **kw):
        return self._respond("DELETE", path, kw.get("params"))

    def ds_proxy(self, uid, path, **kw):
        return self._respond("GET", f"/datasources/proxy/uid/{uid}/{path.lstrip('/')}", kw.get("params"))

    def health(self):
        return self._respond("GET", "/health", None)

    def search(self, *, params=None, limit=0):
        data = self._respond("GET", "/search", params)
        return iter(data if isinstance(data, list) else [])

    def close(self):
        pass


@pytest.fixture
def fake_client():
    return FakeClient
