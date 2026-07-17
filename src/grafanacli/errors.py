"""Grafana-specific additions to the shared exit-code taxonomy.

The base contract lives in :mod:`agentcli.errors` and is identical across every
tool in the family — 0 ok · 1 generic · 3 config · 4 auth · 5 not-found ·
6 conflict · 7 validation · 130 SIGINT. Import from here so a command module has
one place to look.

**0-7 are the shared contract; 8+ is each tool's own namespace.** So Grafana's 8
and Drone's 8 mean different things, and that is fine: an agent always knows
which binary it invoked. What must never happen is a tool redefining 0-7 — an
agent that learned "6 means conflict" from OpenProject must never meet a CLI
where it means something else.

Both additions below were earned by observation against a live Grafana 13.0.3,
not guessed:
"""

from __future__ import annotations

from agentcli.errors import (  # noqa: F401  (re-exported for command modules)
    ApiError,
    AuthError,
    ConfigError,
    ConflictError,
    DryRun,
    NotFoundError,
    OpError,
    ValidationError,
)


class DatasourceUnreachable(OpError):
    """Grafana is healthy, but the datasource's own backend is not.

    Exit **8**. Verified live: a Loki datasource pointing at a proxy that was
    down returned, through ``/api/datasources/proxy/uid/{uid}/…``::

        HTTP/2 502
        content-length: 0

    A 502 with an **empty body** — so any client that assumes "error responses
    carry JSON" dies in ``json.loads`` instead of reporting the problem. (Mine
    did, which is how this is documented.)

    It needs its own code because the fix has nothing to do with the CLI's own
    auth or config: your token is fine, your URL is fine, Grafana answered — the
    thing *behind* the datasource is down. Folding this into ``ApiError`` sends
    people to re-check their token, which is exactly the wrong place.

    Deliberately a sibling of ``ApiError``, not a subclass: an ``except ApiError``
    ladder that swallowed this would report "the API failed" and hide which of
    the two systems actually did.
    """

    exit_code = 8


class OrgMismatch(OpError):
    """The token is valid — for a different organisation.

    Exit **9**. Verified live, in both directions: a service-account token from
    org 1 sent with ``X-Grafana-Org-Id: 6`` (and vice versa) returns::

        401 {"message":"API key does not belong to the requested organization",
             "messageId":"api-key.organization-mismatch"}

    Service-account tokens are **hard-scoped to one org**. There is no header, no
    ``/api/user/using/{id}`` call, no flag that widens them: cross-org access
    means a second token, which is why this CLI does multi-org with one **profile
    per org** (`grafana-cli auth login --profile sales`).

    A sibling of ``AuthError``, not a subclass, and that is the whole point: an
    agent that catches ``AuthError`` will try to re-authenticate, and re-running
    `auth login` with the *same* token fixes nothing here. "Your credential is
    bad" and "your credential is for the wrong org" are different problems with
    different fixes, so they get different codes.
    """

    exit_code = 9
