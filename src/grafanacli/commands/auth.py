"""`graf auth` — log in, log out, inspect credentials.

Two things make this different from the sibling CLIs' `auth.py`, and both trace
back to one Grafana fact: a service-account token is hard-scoped to exactly one
org (verified live — see `errors.OrgMismatch`).

* **`login` never asks for an org.** It cannot ask honestly: the org is a
  property of the token, fixed at mint time, and a typed value would either
  match by luck or fail on the first real request. So it is *discovered*
  (`GET /api/org`, after verifying with `GET /api/user`) and stored on the
  profile — never solicited.
* **Multi-org is one profile per org**, not a flag. `graf auth login --profile
  sales` with a token minted in that org is how a second org gets added; there
  is no header, no switch, nothing that widens a single token's reach.
"""

from __future__ import annotations

import sys

import typer

from ..client import Client
from ..config import Config, Profile
from ..errors import AuthError, ConfigError, OpError
from ..spec import SPEC, credentials, token_url
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)


def _prompt(text: str, *, secret: bool = False) -> str:
    """Prompt on stderr so stdout stays a clean machine channel."""
    if secret:
        import getpass

        return getpass.getpass(text, stream=sys.stderr).strip()
    sys.stderr.write(text)
    sys.stderr.flush()
    return (sys.stdin.readline() or "").strip()


def _normalize_server(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        raise ConfigError("a server URL is required, e.g. https://grafana.example.com")
    if not url.startswith(("http://", "https://")):
        # Nobody types the scheme. Assume TLS rather than rejecting.
        url = "https://" + url
    return url


@app.command()
def login(
    ctx: typer.Context,
    url: str = typer.Option(None, "--url", "-u", help="Grafana server URL, e.g. https://grafana.example.com."),
    token: str = typer.Option(
        None, "--token", "-t", help="Service-account token. Get one at <url>/org/serviceaccounts."
    ),
    profile: str = typer.Option(
        None,
        "--profile",
        help=(
            "Profile to write. One profile per Grafana ORG — a token cannot cross orgs, "
            "so a second org means logging in again with --profile <name> and a token "
            "minted THERE. Defaults to the currently active profile."
        ),
    ),
    insecure: bool = typer.Option(False, "--insecure", help="Skip TLS verification (self-signed certs)."),
) -> None:
    """Log in and store the token in your OS keyring.

    Interactive when flags are omitted: asks for the server, then — knowing the
    URL — shows exactly where to get a token, rather than making you hunt for it.

    The org is never asked for; see the module docstring for why. It is read
    back from the token itself (`GET /api/org`) and stored on the profile.
    """
    obj = ctx_obj(ctx)

    if not url:
        if not sys.stdin.isatty():
            raise ConfigError("--url is required when stdin is not a terminal.")
        sys.stderr.write("\nGrafana server URL (e.g. https://grafana.example.com)\n")
        url = _prompt("Server: ")
    server = _normalize_server(url)

    if not token:
        if not sys.stdin.isatty():
            raise ConfigError("--token is required when stdin is not a terminal.")
        # The whole point: we know the server now, so show the exact page — and
        # say WHICH role, since that is the one setup choice with consequences.
        #
        # The roles below are MEASURED against Grafana 13.0.3 (pinned by
        # tests/test_roles_live.py), and that matters: this prompt used to tell
        # people to use Admin, reasoning that only an Admin could enumerate
        # datasources. The reasoning was wrong — a Viewer enumerates fine — so the
        # prompt was talking users into granting a CLI org-administration rights it
        # has never once used. Least privilege is the entire point of a service
        # account; a setup prompt that inflates it is a security bug, not a typo.
        sys.stderr.write(
            f"\nCreate a service account at:\n  {token_url(server)}\n\n"
            "Then \"Add token\" — the value is shown ONCE, so copy it before leaving "
            "the page.\n\n"
            "Which role:\n"
            "  Viewer  — enough to DISCOVER and READ: `logs sources`, `logs query`,\n"
            "            `metrics`, `scan`, `alert route`. Pick this if you only read.\n"
            "  Editor  — the above, plus creating dashboards, alert rules and contact\n"
            "            points. Pick this if you want `graf alert create`.\n"
            "  Admin   — grants org administration this tool never uses. Not needed.\n\n"
        )
        token = _prompt("Token: ", secret=True)
    if not token:
        raise ConfigError("a token is required.")

    # Verify BEFORE persisting: storing a bad token just moves the failure to a
    # later, more confusing command that no longer has "I just typed this" context.
    client = Client(server, token, verify_ssl=not insecure)
    try:
        user = client.get("/user")
    except AuthError as exc:
        raise AuthError(
            f"that token was rejected by {server}. Get a fresh one at {token_url(server)}.",
            detail=getattr(exc, "detail", None),
        ) from exc

    # The org is a property of the TOKEN — discover it, never ask for it (see
    # the module docstring). Best-effort: a token that authenticates but somehow
    # cannot read its own org should still be allowed to log in; the org fields
    # are then informational blanks rather than a login blocker.
    try:
        org = client.get("/org")
    except OpError:
        org = {}

    name = profile or obj.config.active_profile_name()
    cfg: Config = obj.config
    cfg.upsert_profile(
        Profile(
            name=name,
            base_url=server,
            org_id=org.get("id"),
            org_name=org.get("name"),
            username=user.get("login"),
            verify_ssl=not insecure,
        ),
        make_current=True,
    )
    backend = credentials.store_token(name, token)
    cfg.save()

    obj.emitter.emit(
        {
            "status": "logged in",
            "server": server,
            "profile": name,
            "orgId": org.get("id"),
            "orgName": org.get("name"),
            "login": user.get("login"),
            "credentialBackend": backend,
        }
    )


@app.command()
def status(ctx: typer.Context) -> None:
    """Show the active profile, its org, and — crucially — WHICH token/backend
    is actually in use.

    Precedence is env > keyring > file (see `credentials.py`), deliberately: it
    is what lets this tool run non-interactively in CI without touching a
    keyring that isn't there. But that also means an exported `GRAFANA_TOKEN`
    silently overrides a keyring `graf auth login`, confusing exactly when you
    can least afford it — so this command always names the backend that will
    actually speak, never just whether *a* token exists.
    """
    obj = ctx_obj(ctx)
    cfg: Config = obj.config
    name = cfg.active_profile_name()
    try:
        prof = cfg.resolve()
        server = prof.base_url
        org_id = prof.org_id
        org_name = prof.org_name
    except ConfigError:
        server = org_id = org_name = None

    tok = credentials.get_token(name)
    out: dict = {
        "profile": name,
        "server": server,
        "orgId": org_id,
        "orgName": org_name,
        "authenticated": bool(tok),
        "credentialBackend": credentials.backend_name(),
        "tokenEnvVars": list(SPEC.token_env_names()),
        "configPath": str(SPEC.config_file()),
    }
    if server:
        out["tokenUrl"] = token_url(server)

    # Say it out loud when the environment is beating the keyring/file — the
    # exact trap this command exists to catch.
    hit = credentials._env_token_hit()
    if hit and (SPEC.config_file().exists() or cfg.profiles):
        out["note"] = (
            f"${hit[0]} is set and takes precedence over any keyring login. "
            f"Unset it to use your stored credentials."
        )

    if tok:
        try:
            user = obj.client().get("/user")
        except OpError as exc:
            # A stored/env token exists but the server rejects it (revoked, wrong
            # org, server unreachable). Report that; do not crash `auth status`,
            # which is what you run to DIAGNOSE exactly this.
            out["identity"] = None
            out["identityError"] = str(exc)
        else:
            # `id` is 0 and `isGrafanaAdmin` is false for EVERY service account
            # (verified live) — neither means anything, so neither is echoed
            # here; echoing them would invite a caller to gate on one.
            out["identity"] = {"login": user.get("login"), "uid": user.get("uid")}

    obj.emitter.emit(out)


@app.command()
def profiles(ctx: typer.Context) -> None:
    """List every configured profile — the multi-org overview.

    One profile per Grafana org is the whole multi-org model here (see
    `config.py`'s module docstring), so this is the command that answers "which
    orgs do I have set up, and which one is active right now?".
    """
    obj = ctx_obj(ctx)
    cfg: Config = obj.config
    active = cfg.active_profile_name()
    rows = [
        {
            "profile": pname,
            "active": pname == active,
            "server": p.base_url,
            "orgId": p.org_id,
            "orgName": p.org_name,
            "username": p.username,
            "hasToken": bool(credentials.get_token(pname)),
        }
        for pname, p in sorted(cfg.profiles.items())
    ]
    obj.emitter.emit(
        rows,
        columns=[
            ("Profile", "profile"),
            ("Active", lambda r: "yes" if r["active"] else ""),
            ("Server", "server"),
            ("Org", lambda r: f"{r['orgName']} ({r['orgId']})" if r["orgName"] else str(r["orgId"] or "")),
            ("Token", lambda r: "yes" if r["hasToken"] else "no"),
        ],
        empty="(no profiles configured — run `graf auth login`)",
    )


@app.command()
def logout(
    ctx: typer.Context,
    profile: str = typer.Option(None, "--profile", help="Profile to log out (default: the active one)."),
) -> None:
    """Remove the stored token for a profile.

    The profile's server/org config is left in place — `graf auth login
    --profile <name>` re-populates just the token, without re-typing the URL.
    """
    obj = ctx_obj(ctx)
    name = profile or obj.config.active_profile_name()
    credentials.delete_token(name)
    obj.emitter.emit({"status": "logged out", "profile": name})
