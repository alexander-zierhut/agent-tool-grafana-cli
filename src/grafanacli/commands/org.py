"""`graf org` — organisations, and why a token only ever sees one.

This group exists to explain a constraint, not to browse a resource. Grafana's
own multi-tenancy model invites the wrong mental model: "an org is a thing you
list and switch between", the way you might switch Kubernetes contexts. A
service-account token cannot do that — it is minted inside exactly one org and
stays there for its whole life. Verified live, in both directions: a token
from org 1 sent with `X-Grafana-Org-Id: 6` (and the reverse) gets a hard
401 `api-key.organization-mismatch`. There is no header, no
`/api/user/using/{id}` call, no flag that widens it.

So the three commands here answer three different, narrower questions than
"list my orgs":

* `current`  — which org (and which identity) is THIS token/profile using?
* `list`     — which orgs can THIS CLI reach AT ALL — which, honestly, means
               "which orgs have a configured profile", because the API has no
               way for a normal token to enumerate every org on the server.
* `check`    — does the active profile's on-disk record of its org agree with
               what the token actually is? Catches "I pasted the wrong token
               into this profile" before a command runs and hands back
               confidently-wrong data under the wrong org's name.

Two endpoints that look like they answer "what orgs exist" and do not:

* `GET /api/orgs` (every org on the server) needs **server** admin, not org
  admin — verified live: 403 even though this token's own
  `/api/access-control/user/permissions` lists `orgs:read` with scope `[""]`.
  Permission presence is not capability; the scope is what is actually
  checked, and server-admin routes ignore org-scoped permissions entirely.
* `GET /api/user/orgs` (the orgs THIS user belongs to) returns **304 with an
  empty body** for a service account, live. Not degraded, not filtered —
  useless. This CLI does not call it.

Multi-org therefore means **one profile per org**
(`graf auth login --profile sales`), and `context` is deliberately keyed by
profile (see `config.py`) rather than global, because a datasource uid from
org 1 means nothing in org 6.
"""

from __future__ import annotations

import typer

from ..errors import OpError, OrgMismatch
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)


@app.command()
def current(ctx: typer.Context) -> None:
    """The org this token is scoped to, and who the token is.

    Joins `GET /api/org` (the org) with `GET /api/user` (the identity). On a
    service account, `id` is always **0** and `isGrafanaAdmin` is always
    **false** — verified live, and true even for a token with the org Admin
    role, because server-admin and org-admin are different axes. Neither is
    reported as a capability signal here; `isServiceAccount` reflects `id ==
    0` purely as a fact about the token kind, not a permission check.

    If the profile has an org id on record and it disagrees with what the
    token actually returns, that is flagged inline — but note this can only
    happen when the profile's org id was never asserted on the wire (see
    `config.Profile` / `client._headers`): if it WAS asserted, the mismatch
    would already have failed loudly as `OrgMismatch` before this command's
    own `/api/org` call ever returned. `org check` is the command that turns
    that failure into a report instead of an error.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    org = client.get("/org")
    user = client.get("/user")
    prof = obj.config.resolve()

    out = {
        "org": {"id": org.get("id"), "name": org.get("name")},
        "identity": {
            "login": user.get("login"),
            "uid": user.get("uid"),
            "isServiceAccount": user.get("id") == 0,
        },
        "profile": {"name": prof.name, "configuredOrgId": prof.org_id},
    }
    if prof.org_id is not None and org.get("id") is not None and prof.org_id != org.get("id"):
        out["mismatch"] = (
            f"profile {prof.name!r} records org {prof.org_id}, but /api/org just answered org "
            f"{org.get('id')} for the same token. Run `graf org check` for the full picture."
        )
    obj.emitter.emit(out)


@app.command("list")
def list_(ctx: typer.Context) -> None:
    """Every org this CLI can reach — NOT every org that exists on the server.

    For a normal (non-server-admin) token those are different questions and
    only the first one is answerable: `GET /api/orgs` needs server admin and
    403s for everyone else (see module docstring). So the honest answer here
    is **the configured profiles** — one per org, by construction (see
    `config.py`) — each with the org id/name recorded when you logged in, and
    which one is currently active.

    `GET /api/orgs` is still attempted, once, as a bonus: if this token
    happens to be server admin it succeeds, and the result is folded in under
    `allOrgs`, clearly labelled with the endpoint that produced it. Any
    failure there (403 for the common case, or even an `OrgMismatch` if the
    active profile's own record is stale) is swallowed silently — it is a
    bonus rung, and its absence is not news; the profile list above is the
    real answer regardless.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    active = obj.config.active_profile_name()

    profiles = [
        {
            "profile": name,
            "orgId": prof.org_id,
            "orgName": prof.org_name,
            "baseUrl": prof.base_url,
            "active": name == active,
        }
        for name, prof in obj.config.profiles.items()
    ]
    profiles.sort(key=lambda p: (not p["active"], p["profile"]))

    out: dict = {
        "profiles": profiles,
        "note": (
            "this is \"every org you have a profile for\", not \"every org on the server\": "
            "service-account tokens are scoped to one org each, and GET /api/orgs (every org) "
            "needs SERVER admin, which most tokens are not. One profile per org is how this CLI "
            "does multi-org: `graf auth login --profile <name>` for each one."
        ),
    }
    try:
        all_orgs = client.get("/orgs")
    except OpError:
        # Expected for anything short of server admin — see module docstring.
        # Not worth reporting as a failure; `note` above already explains why
        # this rung is usually empty. Deliberately broad (OpError, not just
        # AuthError) so a stale profile's OrgMismatch is swallowed here too:
        # this is a bonus probe, not the command's actual job.
        pass
    else:
        if isinstance(all_orgs, list):
            out["allOrgs"] = all_orgs
            out["allOrgsSource"] = "GET /api/orgs (server-admin only; succeeded for this token)"
    obj.emitter.emit(out)


@app.command()
def check(ctx: typer.Context) -> None:
    """Does the active profile's recorded org match what its token actually is?

    Catches "I copied the wrong token into this profile" — the config still
    says org 6, but the token pasted in during a later `auth login` actually
    belongs to org 1. Left unchecked, every subsequent command either fails
    loudly (if the profile's org id gets asserted on the wire and disagrees —
    see `client._headers`) or, if the profile never recorded an org id at all,
    succeeds silently against whatever org the token happens to belong to,
    handing back confidently-wrong data under no particular org's name.

    Modelled on `server doctor`: **this never lets `OrgMismatch` escape as a
    raw error.** It is the one command in this group whose entire job is
    diagnosing that exact failure, so it catches it and turns it into a
    reported `ok: false` instead — deliberately, and only for this one
    exception. Anything else (a flat-out bad token, a network failure) is a
    different problem with a different fix and is left to propagate as
    itself, exit code and all, rather than folded into this report.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    prof = obj.config.resolve()

    try:
        org = client.get("/org")
    except OrgMismatch as exc:
        obj.emitter.emit(
            {
                "profile": prof.name,
                "configuredOrgId": prof.org_id,
                "ok": False,
                "problem": str(exc),
            }
        )
        return

    actual_id = org.get("id")
    out = {
        "profile": prof.name,
        "configuredOrgId": prof.org_id,
        "actualOrgId": actual_id,
        "actualOrgName": org.get("name"),
        "ok": True,
    }
    if prof.org_id is None:
        out["note"] = (
            f"profile {prof.name!r} has no org id on record, so nothing was asserted on the wire "
            f"for this call — the token itself belongs to org {actual_id} ({org.get('name')}). "
            f"Not a problem, but worth recording: `graf auth login --profile {prof.name}` "
            f"re-derives it."
        )
    obj.emitter.emit(out)
