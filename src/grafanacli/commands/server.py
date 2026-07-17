"""`grafana-cli server` — is this server healthy, whose token is speaking, and what can
it actually do?

`doctor` is the whole reason this module exists. Grafana collapses a lot of
unrelated failures into the same shape of error ("401" with a message that
sometimes helps, sometimes doesn't), and each one needs a DIFFERENT fix: a
wrong URL is not a bad token, a bad token is not the wrong org, and a token for
the right org can still be too weak for the thing you are about to try. Doctor
chains five probes, in an order that is itself the design — each rung isolates
one failure before the next rung can get confused by it:

    /api/health (unauth)  -> is there even a Grafana here, and what version?
    /api/user              -> is the token valid, and who is it?
    /api/org                -> valid for a token, but is it THIS org?
    /api/access-control/user/permissions -> what can it actually do (honestly)?
    datasource enumeration  -> and can it reach the things it would query?

**Doctor must never raise.** Returning the report IS its job — an escaping
exception hands the operator a traceback instead of a diagnosis, i.e. exactly
where they were before running it. The sibling trap is the usual way this goes
wrong: in :mod:`agentcli.errors`, ``NotFoundError`` and ``ApiError`` are
SIBLINGS (both subclass ``OpError`` directly), so an ``except ApiError`` ladder
that looks exhaustive silently is not — a stray 404 sails right through it. This
exact trap leaked a raw error out of drone's `server doctor`; every probe below
ends in an ``except OpError`` floor, and :func:`_run_probe` is the floor under
that floor for whatever a probe forgets.
"""

from __future__ import annotations

import time
from typing import Callable

import typer
from agentcli.errors import ApiError, AuthError, ConfigError, NotFoundError, OpError

from .. import __version__, sources
from ..client import Client
from ..errors import DatasourceUnreachable, OrgMismatch
from ..spec import SPEC, credentials
from ._shared import ctx_obj, need_window

app = typer.Typer(no_args_is_help=True, help="Health, version — and `server doctor`.")

#: doctor's "the report says unhealthy" signal, opt-in only (`--exit-code`).
#: Deliberately far from the 0-9 error-code band (the same convention as
#: drone's `build wait --exit-code`, 20-29): doctor DIAGNOSING a broken server
#: is a successful run of this command, not a failure of it. A different band
#: from `metrics up`'s EXIT_TARGETS_DOWN (20) on purpose — they are different
#: commands, and an agent always knows which one it ran, the same reasoning
#: that lets Grafana's exit code 8 and Drone's exit code 8 mean different things.
EXIT_UNHEALTHY = 21

# Facts an agent would otherwise learn by probing wrong, or not at all. Reported,
# never (re-)probed here — doctor stays read-only, so it is safe to run against
# a server that is already on fire.
_CAPABILITY_NOTES = [
    "GET /api/user/orgs answers 304 with an EMPTY body for a service account — useless, and "
    "not probed here.",
    "GET /api/orgs (every org on the server) needs SERVER admin and 403s for a normal service "
    "account. GET /api/org (just the current one) is the one that works, and the one this "
    "doctor uses for the 'org' check.",
    "id=0 and isGrafanaAdmin=false on /api/user are NORMAL for a service account and are not "
    "capability signals — see the 'token' check's note.",
    "A permission NAME being present in /api/access-control/user/permissions is not capability "
    "— the SCOPE is (our own token carries orgs:read with scope [\"\"] and /api/orgs still "
    "403s). Alert-rule write access is folder-scoped; see the 'permissions' check for which "
    "folders.",
]


def _open_client(obj) -> tuple[Client, str | None]:
    """A client that works with NO token.

    `AppContext.client()` refuses to build one without a token — correct for
    every other command, wrong here: `/api/health` is unauthenticated
    specifically so `server health`/`server doctor` can answer even before
    `grafana-cli auth login` has ever run. That is the whole value of rung 1 (it
    separates "wrong URL" from "bad token"), so this bypasses the normal gate.
    """
    prof = obj.config.resolve()
    token = credentials.get_token(obj.config.active_profile_name())
    client = Client(
        prof.base_url,
        token or "",
        org_id=prof.org_id,
        verify_ssl=prof.verify_ssl,
        dry_run=False,  # read-only commands; nothing here is ever worth intercepting
        user_agent=f"agent-tool-grafana-cli/{__version__}",
    )
    return client, token


def _credential_report(obj, token: str | None) -> dict:
    """WHICH credential is actually speaking — the fact people lose the most
    time to. Precedence is env > keyring > file (deliberate — it is what makes
    CI work headless), so an exported `$GRAFANA_TOKEN`/`$GRAFANACLI_TOKEN`
    silently outranks a keyring login done with `grafana-cli auth login`.
    """
    out = {
        "backend": credentials.backend_name(),
        "profile": obj.config.active_profile_name(),
        "tokenEnvVars": list(SPEC.token_env_names()),
        "present": bool(token),
    }
    hit = credentials._env_token_hit()
    if hit:
        out["note"] = (
            f"authenticating with ${hit[0]}, NOT the keyring — the environment always wins. "
            f"If you ran `grafana-cli auth login` and this doesn't look like that login, "
            f"`unset {hit[0]}` to fall back to the stored one."
        )
    return out


# ---------------------------------------------------------------------------
# probes — pure over a client, one diagnosis per failure shape
# ---------------------------------------------------------------------------


def _run_probe(check: str, fn: Callable[[], dict]) -> dict:
    """Run a probe and turn ANYTHING that escapes into a reported failure.

    Every probe below already ends in an `except OpError` floor; this is the
    floor under THAT floor. Doctor's whole contract is "hand back a report" —
    the cost of a probe leaking is not a bad row in the output, it is the
    operator getting a Python traceback instead of a diagnosis, for a server
    that may already be on fire. A bare `except Exception` is the right blast
    radius here precisely because the report says which probe broke, so the
    gap is visible in the JSON rather than swallowed by a crash.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — deliberate, see the docstring
        return {
            "check": check, "ok": False, "diagnosis": "probe-failed",
            "message": (
                f"the {check} probe itself raised {type(exc).__name__}: {exc}. That is a gap "
                f"in this CLI, not proof about the server — the other checks still hold."
            ),
        }


def probe_health(client: Client) -> dict:
    """Rung 1: `GET /api/health` — unauthenticated, so it separates "the URL is
    wrong / the server is down" from "your token is bad" before auth is ever
    involved. Reports the version so every other check has it for context.
    """
    try:
        res = client.health()
    except NotFoundError as exc:
        return {
            "check": "reachable", "ok": False, "diagnosis": "not-a-grafana-server",
            "message": (
                f"{client.web_root} answered, but GET /api/health is a 404 — every Grafana "
                f"serves it unauthenticated. This is some other service, or a proxy that is "
                f"not routing to Grafana. Server said: {exc}"
            ),
        }
    except AuthError as exc:
        return {
            "check": "reachable", "ok": False, "diagnosis": "not-a-grafana-server",
            "message": (
                f"{client.web_root}/api/health demanded authentication. Grafana never does — "
                f"something in front of it (a proxy, an SSO gateway) is intercepting the "
                f"request. Server said: {exc}"
            ),
        }
    except ApiError as exc:
        return {
            "check": "reachable", "ok": False, "diagnosis": "unreachable",
            "message": (
                f"cannot reach {client.web_root}: {exc}. Check the URL, DNS, TLS, and that the "
                f"server is actually up."
            ),
        }
    except OpError as exc:
        # The floor: any other OpError (a 400, a 501, ...) reaches here. Something
        # answered, and it did not answer the way any Grafana does.
        return {
            "check": "reachable", "ok": False, "diagnosis": "not-a-grafana-server",
            "message": (
                f"{client.web_root}/api/health answered, but not the way any Grafana does "
                f"({type(exc).__name__}: {exc})."
            ),
        }
    if not isinstance(res, dict) or "version" not in res:
        return {
            "check": "reachable", "ok": False, "diagnosis": "not-a-grafana-server",
            "message": (
                f"{client.web_root}/api/health answered, but not with Grafana's shape "
                f"({str(res)[:120]!r}). Something else is answering at this URL — check for a "
                f"typo'd port, or a proxy routing you somewhere unexpected."
            ),
        }
    return {
        "check": "reachable", "ok": True, "diagnosis": None,
        "message": f"{client.web_root} is Grafana {res.get('version')} (database: {res.get('database')}).",
        "version": res.get("version"), "database": res.get("database"), "commit": res.get("commit"),
    }


def probe_identity(client: Client) -> dict:
    """Rung 2: `GET /api/user` — is the token valid, and who is it?

    Verified live: for a service account, `id` is 0 and `isGrafanaAdmin` is
    false EVEN WHEN the account holds the org Admin role — server-admin
    (`isGrafanaAdmin`) and org-admin are different flags, and gating a
    diagnosis on either would call a perfectly capable token crippled. Neither
    is used here; `uid` (`service-account:N`) is what actually identifies it.
    """
    try:
        me = client.get("/user")
    except OrgMismatch as exc:
        # A sibling of AuthError, not a subclass — the header assertion in
        # client.py fires on every authenticated call once org_id is
        # configured, so this is often where a wrong-org profile is caught
        # FIRST, before the dedicated 'org' check ever runs.
        return {
            "check": "token", "ok": False, "diagnosis": "org-mismatch",
            "message": str(exc), "detail": getattr(exc, "detail", None),
        }
    except AuthError as exc:
        return {
            "check": "token", "ok": False, "diagnosis": "bad-token",
            "message": f"the token was rejected (server said: {exc}). Get a fresh one with `grafana-cli auth login`.",
        }
    except OpError as exc:
        return {
            "check": "token", "ok": False, "diagnosis": "probe-failed",
            "message": (
                f"GET /api/user failed in a way this probe cannot classify "
                f"({type(exc).__name__}: {exc}). Treat the token as unverified, not bad."
            ),
        }

    if not isinstance(me, dict) or not me.get("uid"):
        return {
            "check": "token", "ok": False, "diagnosis": "unexpected-response",
            "message": f"GET /api/user returned something without a `uid` field: {str(me)[:160]!r}.",
        }
    return {
        "check": "token", "ok": True, "diagnosis": None,
        "message": f"authenticated as {me.get('login') or me.get('uid')} in org {me.get('orgId')}.",
        "uid": me.get("uid"), "login": me.get("login"), "orgId": me.get("orgId"),
        "note": (
            "id/isGrafanaAdmin are not reliable capability signals for a service account (id is "
            "always 0; isGrafanaAdmin is server-admin, a different thing from org Admin) — see "
            "the 'permissions' check for what this token can actually do."
        ),
    }


def probe_org(client: Client, profile) -> dict:
    """Rung 3: `GET /api/org` — valid for A token, but is it valid for THIS org?

    In the common case a mismatch already failed rung 2 — `X-Grafana-Org-Id`
    is asserted on every authenticated call once the profile has an `org_id`
    configured, so Grafana 401s before this rung ever runs. This stays a
    SEPARATE, explicit check anyway for the case that assertion does not cover:
    a profile that has never set `org_id` sends no header at all, sails through
    rung 2 with whatever org the token happens to be in, and would silently
    trust it forever. This is where that gets said out loud.
    """
    try:
        res = client.get("/org")
    except OrgMismatch as exc:
        return {"check": "org", "ok": False, "diagnosis": "org-mismatch", "message": str(exc)}
    except AuthError as exc:
        return {"check": "org", "ok": False, "diagnosis": "bad-token", "message": str(exc)}
    except OpError as exc:
        return {
            "check": "org", "ok": False, "diagnosis": "probe-failed",
            "message": f"GET /api/org failed ({type(exc).__name__}: {exc}).",
        }

    if not isinstance(res, dict):
        return {
            "check": "org", "ok": False, "diagnosis": "unexpected-response",
            "message": f"GET /api/org returned something unexpected: {str(res)[:120]!r}.",
        }

    actual_org, configured = res.get("id"), profile.org_id
    out = {
        "check": "org", "ok": True, "diagnosis": None,
        "message": f"token is in org {actual_org} ({res.get('name')}).",
        "orgId": actual_org, "orgName": res.get("name"), "configuredOrgId": configured,
    }
    if configured is not None and actual_org is not None and int(configured) != int(actual_org):
        # Should be unreachable when `org_id` is set (rung 2 asserts it first),
        # but this is cheap insurance against a version where that assertion
        # does not fire the way `client.py` assumes — see the docstring above.
        out.update(
            ok=False, diagnosis="org-mismatch",
            message=(
                f"the profile is configured for org {configured}, but the token answers as org "
                f"{actual_org} ({res.get('name')}). Re-running `grafana-cli auth login` with the SAME "
                f"token fixes nothing here — mint a token in org {configured}, or point this "
                f"profile at org {actual_org}."
            ),
        )
    return out


def probe_permissions(client: Client) -> dict:
    """Rung 4: `GET /api/access-control/user/permissions` — the HONEST
    capability probe.

    Verified live: a permission NAME being present is not proof of capability
    — this exact token carries `orgs:read` with scope `[""]` and `/api/orgs`
    still 403s, because the scope is the answer and the name alone is not. Alert
    rule write access is folder-scoped (`alert.rules:create` with scope
    `folders:uid:X`), so this reports WHICH folders, not just yes/no.
    """
    try:
        perms = client.get("/access-control/user/permissions")
    except AuthError as exc:
        return {
            "check": "permissions", "ok": False, "diagnosis": "insufficient-permission",
            "message": (
                f"cannot even read this token's OWN permission list ({exc}). Every capability "
                f"below is UNKNOWN, not merely absent."
            ),
        }
    except OpError as exc:
        return {
            "check": "permissions", "ok": False, "diagnosis": "probe-failed",
            "message": f"GET /api/access-control/user/permissions failed ({type(exc).__name__}: {exc}).",
        }

    if not isinstance(perms, dict):
        return {
            "check": "permissions", "ok": False, "diagnosis": "unexpected-response",
            "message": f"the permissions endpoint returned something unexpected: {str(perms)[:120]!r}.",
        }

    def scopes(name: str) -> list[str]:
        v = perms.get(name)
        return v if isinstance(v, list) else []

    write_scopes = scopes("alert.rules:create")
    writeable_folders = sorted(
        {s.split("folders:uid:", 1)[1] for s in write_scopes if s.startswith("folders:uid:")}
    )
    caps = {
        "listDatasources": bool(scopes("datasources:read")),
        "readAlertRules": bool(scopes("alert.rules:read")),
        "writeAlertRules": bool(writeable_folders) or "*" in write_scopes,
        "writeableFolders": writeable_folders,
        "manageContactPoints": bool(scopes("alert.notifications.receivers:write")),
    }
    return {
        "check": "permissions", "ok": True, "diagnosis": None,
        "message": (
            f"listDatasources={caps['listDatasources']}, "
            f"writeAlertRules={caps['writeAlertRules']} (folders: {', '.join(writeable_folders) or 'none'}), "
            f"manageContactPoints={caps['manageContactPoints']}."
        ),
        "capabilities": caps,
        "permissionCount": len(perms),
    }


def probe_datasources(client: Client, window) -> dict:
    """Rung 5: every datasource in this org — type, and whether THIS CLI can
    reach it.

    Needs `datasources:read` (see rung 4); if that is missing, `client.py`
    already turns the 403 into a message naming the permission, and that
    message is what gets reported here rather than a bare "forbidden". Each
    datasource's reachability is captured, never raised — the live instance has
    exactly one whose backend is down (502, empty body), and that must show up
    as a clear per-row failure beside the healthy ones, not blank out the report.
    """
    try:
        all_ds = sources.list_datasources(client)
    except OpError as exc:
        return {
            "check": "datasources", "ok": False, "diagnosis": "probe-failed",
            "message": f"GET /api/datasources failed ({type(exc).__name__}: {exc}).",
        }

    rows: list[dict] = []
    for ds in all_ds:
        row = sources.classify(ds)
        if row["logs"] == "supported":
            try:
                row.update(sources.describe_loki(client, str(row["uid"]), window))
                row["reachable"] = True
            except DatasourceUnreachable as exc:
                row["reachable"], row["error"] = False, str(exc)
            except AuthError as exc:
                row["reachable"], row["error"] = False, str(exc)
            except OpError as exc:
                row["reachable"], row["error"] = False, str(exc)
        elif row["metrics"] == "supported":
            try:
                client.ds_proxy(str(row["uid"]), "api/v1/query", params={"query": "up", "time": time.time()})
                row["reachable"] = True
            except DatasourceUnreachable as exc:
                row["reachable"], row["error"] = False, str(exc)
            except AuthError as exc:
                row["reachable"], row["error"] = False, str(exc)
            except OpError as exc:
                row["reachable"], row["error"] = False, str(exc)
        else:
            row["reachable"] = None
            row["note"] = (
                "recognised but not queryable by this CLI yet" if (row["logs"] or row["metrics"])
                else "not a log or metrics datasource"
            )
        rows.append(row)

    unreachable = [r["name"] for r in rows if r.get("reachable") is False]
    ok = not unreachable
    return {
        "check": "datasources", "ok": ok,
        "diagnosis": None if ok else "datasource-unreachable",
        "message": (
            f"{len(rows)} datasource(s) in this org, {len(unreachable)} unreachable."
            if rows else "no datasources in this org."
        ),
        "unreachable": unreachable,
        "datasources": rows,
    }


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


@app.command()
def health(ctx: typer.Context) -> None:
    """`GET /api/health` — unauthenticated database + version probe.

    Needs no token, so it is what to run first when nothing else is working:
    does the URL even point at a live Grafana? (For the full diagnosis chain,
    including auth and permissions, use `server doctor`.)
    """
    obj = ctx_obj(ctx)
    client, _ = _open_client(obj)
    res = client.health()
    if not isinstance(res, dict):
        raise OpError(
            f"{client.web_root}/api/health did not return JSON: {str(res).strip()[:120]!r}. "
            f"This may not be a Grafana server."
        )
    obj.emitter.emit({"server": client.web_root, **res})


@app.command()
def version(ctx: typer.Context) -> None:
    """The Grafana server's version — from the same unauthenticated
    `/api/health` probe as `server health`, trimmed to the one field most
    scripts actually want.
    """
    obj = ctx_obj(ctx)
    client, _ = _open_client(obj)
    res = client.health()
    if not isinstance(res, dict):
        raise OpError(
            f"{client.web_root}/api/health did not return JSON: {str(res).strip()[:120]!r}. "
            f"This may not be a Grafana server."
        )
    obj.emitter.emit({"server": client.web_root, "version": res.get("version")})


@app.command()
def doctor(
    ctx: typer.Context,
    exit_code: bool = typer.Option(
        False, "--exit-code",
        help=(
            f"Exit {EXIT_UNHEALTHY} if the report is not fully healthy. Default: always exit 0 — "
            f"diagnosing a broken server correctly is still a SUCCESSFUL run of this command."
        ),
    ),
) -> None:
    """Diagnose the server, the token, its org, and what it can actually do —
    in that order, because each rung isolates one failure before the next rung
    can be confused by it. Named diagnoses:

    \b
      unreachable             wrong URL / DNS / TLS, or the server is down
      not-a-grafana-server    something answered, but it is not Grafana
      bad-token                the token is wrong or was revoked
      org-mismatch             the token is VALID — for a different org. Exit 9
                               when this is hit for real outside doctor;
                               re-authenticating with the SAME token fixes
                               nothing (service-account tokens cannot change
                               org) — mint one in the right org instead.
      insufficient-permission  the token cannot even read its OWN permissions
      datasource-unreachable   Grafana is fine; a datasource's backend is not.
                               Exit 8 when hit for real outside doctor.
      probe-failed             the CHECK broke, not necessarily the server —
                               read its own message; the OTHER rungs still hold

    Read-only and side-effect free — nothing here is ever POSTed — and this
    command itself never exits non-zero for a bad finding; the report IS the
    deliverable. Opt into a process-exit signal with `--exit-code`; otherwise
    read `status` in the JSON.
    """
    obj = ctx_obj(ctx)

    try:
        client, token = _open_client(obj)
        profile = obj.config.resolve()
    except ConfigError as exc:
        # Doctor is the command you run when things are already broken; an
        # unconfigured profile is a finding to report, not a reason to refuse
        # to report anything at all.
        obj.emitter.emit(
            {
                "status": "failed",
                "credential": {"backend": credentials.backend_name()},
                "checks": [
                    {"check": "config", "ok": False, "diagnosis": "not-configured", "message": str(exc)}
                ],
                "problems": ["not-configured"],
                "notes": _CAPABILITY_NOTES,
            }
        )
        if exit_code:
            raise typer.Exit(code=EXIT_UNHEALTHY)
        return

    checks = [_run_probe("reachable", lambda: probe_health(client))]
    tok = _run_probe("token", lambda: probe_identity(client))
    checks.append(tok)

    # Only proceed once the server is real and the token is accepted — an org
    # check on an already-rejected token just repeats the same failure with a
    # second name.
    org_check = None
    if checks[0]["ok"] and tok["ok"]:
        org_check = _run_probe("org", lambda: probe_org(client, profile))
        checks.append(org_check)

    # And only ask what the token can DO once we know it is speaking to the
    # right org — permissions and datasources are both meaningless answers for
    # a token that turned out to be scoped to a different org entirely.
    if checks[0]["ok"] and tok["ok"] and (org_check is None or org_check["ok"]):
        checks.append(_run_probe("permissions", lambda: probe_permissions(client)))
        window = need_window(obj)
        checks.append(_run_probe("datasources", lambda: probe_datasources(client, window)))

    problems = [c["diagnosis"] for c in checks if c.get("diagnosis")]
    # Reachability, the token, and the org are load-bearing for everything after
    # them — a failure there makes the report FAILED. A permissions gap or one
    # unreachable datasource is real but partial, so it is DEGRADED, not failed;
    # the report still answers most of the question.
    fatal = [c for c in checks if not c["ok"] and c["check"] in ("reachable", "token", "org")]
    degraded = bool(problems) or any(not c["ok"] for c in checks)

    report = {
        "status": "failed" if fatal else ("degraded" if degraded else "ok"),
        "server": client.web_root,
        "credential": _credential_report(obj, token),
        "checks": checks,
        "problems": problems,
        "notes": _CAPABILITY_NOTES,
    }
    obj.emitter.emit(report)

    if exit_code and report["status"] != "ok":
        raise typer.Exit(code=EXIT_UNHEALTHY)
