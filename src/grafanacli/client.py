"""HTTP client for the Grafana API.

Like Drone's, this is deliberately NOT shared with the other agent-tool CLIs.
The family shares the *contract* (exit codes, output shapes, credentials), never
the transport — because every rule below is specific to how Grafana behaves, and
hoisting any of them into shared code would silently break a sibling tool.

What is Grafana-shaped here:

* **Auth** is ``Authorization: Bearer glsa_…`` (service account). Same as Drone,
  different from OpenProject's basic auth.
* **Every request asserts the org.** See ``_headers``.
* **There are two HTTP worlds behind one base URL.** ``/api/...`` is Grafana's own
  API and always answers JSON. ``/api/datasources/proxy/uid/{uid}/...`` is a
  *tunnel* to somebody else's server (Loki, Mimir), which has its own status
  codes, its own JSON shapes, and — verified live — can return **502 with an
  empty body**. Conflating the two is how you get a JSONDecodeError instead of an
  error message.
* **Errors name the permission they need.** Grafana 403 bodies carry
  ``"Permissions needed: alert.rules:create"``, which is far more actionable than
  "forbidden" — so we surface it, and name the role that grants it.
* **Pagination is per-endpoint, not global.** ``/api/search`` takes page+limit;
  the provisioning API returns everything at once; Loki has its own ``limit``.
  There is no single ``paginate()`` that is correct for Grafana, so there isn't
  one — see ``search()``.
"""

from __future__ import annotations

import random
import time
from typing import Any, Iterator

import httpx

from .errors import (
    ApiError,
    AuthError,
    ConflictError,
    DatasourceUnreachable,
    DryRun,
    NotFoundError,
    OrgMismatch,
    ValidationError,
)

_WRITE_METHODS = ("POST", "PATCH", "PUT", "DELETE")
_IDEMPOTENT = ("GET", "HEAD", "PUT", "DELETE")

#: 429 for everything; 5xx only for idempotent methods.
_TRANSIENT_STATUS = {429, 502, 503, 504}

_MAX_ATTEMPTS = 4

#: The prefix that marks a request as a tunnel to a datasource's own backend
#: rather than a call to Grafana itself.
_PROXY_MARKER = "/datasources/proxy/"

#: Grafana's marker for "this token is for another org". Matching the messageId
#: rather than the prose is deliberate: the prose is localised and has been
#: reworded across versions, the messageId has not.
_ORG_MISMATCH_ID = "api-key.organization-mismatch"

#: `/api/search` caps at 5000 per page; 1000 is Grafana's own default and a
#: sane batch.
_SEARCH_PER_PAGE = 1000


class Client:
    """A thin, typed wrapper over Grafana's HTTP API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        org_id: int | None = None,
        verify_ssl: bool = True,
        timeout: float = 30.0,
        dry_run: bool = False,
        user_agent: str = "agent-tool-grafana-cli",
    ) -> None:
        self.api_root = base_url.rstrip("/") + "/api"
        self.web_root = base_url.rstrip("/")
        self.token = token
        self.org_id = org_id
        self.dry_run = dry_run
        self._client = httpx.Client(
            verify=verify_ssl,
            timeout=timeout,
            follow_redirects=True,
            headers=self._headers(token, org_id, user_agent),
        )

    @staticmethod
    def _headers(token: str, org_id: int | None, user_agent: str) -> dict[str, str]:
        """Build the standing headers.

        ``X-Grafana-Org-Id`` is sent whenever the profile knows its org, and it is
        an **assertion, not a switch**. A service-account token cannot change org
        (verified live — the cross-org attempt is a hard 401), so this header can
        never widen access. What it buys is a loud failure instead of a quiet one:
        if the profile says org 6 and the token is org 1's, the very first request
        returns ``api-key.organization-mismatch`` and we say so, rather than
        happily querying org 1 and handing back the wrong org's logs under the
        right org's name.
        """
        h = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": user_agent,
        }
        if org_id is not None:
            h["X-Grafana-Org-Id"] = str(org_id)
        return h

    # ---- plumbing ----------------------------------------------------

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.api_root}/{path.lstrip('/')}"

    @staticmethod
    def _is_proxy(url: str) -> bool:
        return _PROXY_MARKER in url

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
        content: str | bytes | None = None,
        raw: bool = False,
    ) -> Any:
        url = self._url(path)
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        # --dry-run: one chokepoint in the transport, so every write command --
        # present and future -- gets it for free and none can bypass it. Reads
        # still execute: resolving a datasource name to a UID must really happen
        # or the printed request would be a guess.
        if self.dry_run and method.upper() in _WRITE_METHODS:
            raise DryRun(
                {
                    "method": method.upper(),
                    "url": url,
                    "org": self.org_id,
                    "params": clean_params or None,
                    "body": json if json is not None else content,
                }
            )

        proxied = self._is_proxy(url)
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = self._client.request(
                    method, url, params=clean_params or None, json=json, content=content
                )
            except httpx.ConnectError as exc:
                # Never reached the server -> safe to retry regardless of method.
                last_exc = exc
                if attempt == _MAX_ATTEMPTS:
                    raise ApiError(f"cannot reach {self.web_root}: {exc}") from exc
                self._backoff(attempt, None)
                continue
            except httpx.HTTPError as exc:
                raise ApiError(f"request failed: {exc}") from exc

            # A 502 through the datasource proxy is NOT a blip worth retrying:
            # Grafana itself answered (so the network to Grafana is fine) and told
            # us the datasource's *backend* refused. That is an ops/config fact,
            # not a transient one, and it must fail fast -- `logs sources` and
            # `scan` fan out across every datasource, so 4 attempts x N dead
            # datasources turns a clear report into a minute-long stall.
            if proxied and resp.status_code in (502, 503, 504):
                self._raise_datasource_unreachable(resp, url)

            retryable = resp.status_code in _TRANSIENT_STATUS and (
                resp.status_code == 429 or method.upper() in _IDEMPOTENT
            )
            if retryable and attempt < _MAX_ATTEMPTS:
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue

            if resp.status_code >= 400:
                self._raise_for_error(resp, proxied=proxied, url=url)
            if raw:
                return resp.text
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                # Not every response is JSON. Hand back the text rather than
                # exploding in json.loads.
                return resp.text
        raise ApiError(f"request failed after {_MAX_ATTEMPTS} attempts: {last_exc}")

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> None:
        delay = 0.5 * (2 ** (attempt - 1))
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(min(delay, 30.0) + random.uniform(0, 0.25))

    @staticmethod
    def _raise_datasource_unreachable(resp: httpx.Response, url: str) -> None:
        uid = _uid_from_proxy_url(url)
        # Verified live: this arrives with content-length 0, so there is nothing
        # to quote back. Say what is broken and where to look instead.
        raise DatasourceUnreachable(
            f"datasource {uid or '?'} is configured, but its backend did not answer "
            f"(HTTP {resp.status_code} through Grafana's datasource proxy). Grafana "
            f"itself is fine — the server the datasource points at is down or "
            f"unreachable from Grafana. Check the datasource's URL with "
            f"`grafana-cli datasource get {uid or '<uid>'}`.",
            detail={"status": resp.status_code, "datasourceUid": uid, "body": resp.text or None},
        )

    @staticmethod
    def _raise_for_error(resp: httpx.Response, *, proxied: bool = False, url: str = "") -> None:
        status = resp.status_code
        try:
            body = resp.json()
            msg = body.get("message") if isinstance(body, dict) else str(body)
        except ValueError:
            body = resp.text
            msg = (resp.text or "").strip() or f"HTTP {status}"
        message_id = body.get("messageId") if isinstance(body, dict) else None

        # A valid token pointed at the wrong org. Its own code, because "log in
        # again" -- the reflex for any other 401 -- cannot fix it.
        if status == 401 and message_id == _ORG_MISMATCH_ID:
            raise OrgMismatch(
                f"{msg} Service-account tokens are scoped to exactly one org and "
                f"cannot be widened. Use one profile per org: "
                f"`grafana-cli auth login --profile <name>` with a token minted in that org.",
                detail=body,
            )
        if status in (401, 403):
            raise AuthError(_permission_hint(msg) or msg or "unauthorized", detail=body)
        if status == 404:
            if proxied:
                # A 404 from inside the tunnel is the datasource's own 404 (a bad
                # Loki path), not Grafana saying the datasource is missing.
                uid = _uid_from_proxy_url(url)
                raise NotFoundError(
                    f"the datasource {uid or ''} answered 404 for that query path: {msg}".strip(),
                    detail=body,
                )
            raise NotFoundError(msg or "not found", detail=body)
        if status in (409, 412):
            # 412 is Grafana's optimistic-locking status for dashboard saves
            # (version mismatch); 409 shows up for duplicate uid/title.
            raise ConflictError(msg or "conflict", detail=body)
        if status in (400, 422):
            raise ValidationError(msg or "invalid request", detail=body)
        raise ApiError(msg or f"HTTP {status}", status=status, detail=body)

    # ---- verbs -------------------------------------------------------

    def get(self, path: str, **kw) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> Any:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw) -> Any:
        return self.request("PUT", path, **kw)

    def patch(self, path: str, **kw) -> Any:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw) -> Any:
        return self.request("DELETE", path, **kw)

    def health(self) -> Any:
        """Server health + version.

        ``/api/health`` is **unauthenticated** (verified live), which makes it the
        right first rung of `server doctor`: it separates "the server is down or
        the URL is wrong" from "your token is bad" before auth is ever involved.
        """
        return self.request("GET", f"{self.web_root}/api/health")

    # ---- the datasource tunnel ---------------------------------------

    def ds_proxy(self, uid: str, path: str, **kw) -> Any:
        """Call a datasource's own API through Grafana.

        This is the only supported way to reach Loki or Mimir here, and not for
        convenience: on the live instance one org's Loki datasource injects a
        secret header (``X-Loki-Label-Preset``) held in Grafana's
        ``secureJsonFields``, which is **write-only over the API**. The CLI can
        never read it, so it can never reproduce the request by talking to Loki
        directly — and a direct connection would silently skip the scoping that
        header applies, handing back another tenant's logs. Always tunnel.
        """
        return self.request("GET", f"/datasources/proxy/uid/{uid}/{path.lstrip('/')}", **kw)

    # ---- pagination --------------------------------------------------

    def search(self, *, params: dict | None = None, limit: int = 0) -> Iterator[dict]:
        """Yield `/api/search` hits across pages.

        Grafana's search is 1-indexed and returns a bare array with no total, so a
        short page is the terminator — the same rule as Drone's, and the opposite
        of OpenProject's (which has an authoritative `total` and must never stop
        on a short page).
        """
        page = 1
        seen = 0
        per_page = min(_SEARCH_PER_PAGE, limit) if limit else _SEARCH_PER_PAGE
        while True:
            batch = self.get("/search", params={**(params or {}), "page": page, "limit": per_page})
            if not isinstance(batch, list) or not batch:
                return
            for item in batch:
                yield item
                seen += 1
                if limit and seen >= limit:
                    return
            if len(batch) < per_page:
                return  # short page == last page. No total to check against.
            page += 1

    def close(self) -> None:
        self._client.close()


def _uid_from_proxy_url(url: str) -> str | None:
    """Pull the datasource uid back out of a proxy URL, for error messages."""
    marker = "/proxy/uid/"
    if marker not in url:
        return None
    rest = url.split(marker, 1)[1]
    return rest.split("/", 1)[0].split("?", 1)[0] or None


def _permission_hint(msg: Any) -> str | None:
    """Turn Grafana's 403 prose into something a caller can act on.

    Grafana says: "You'll need additional permissions to perform this action.
    Permissions needed: datasources:read". The permission name is the whole
    answer, and it is buried at the end of a sentence that says nothing else. We
    lead with it, and for the handful we have actually hit live we add the fix.
    """
    if not isinstance(msg, str) or "Permissions needed:" not in msg:
        return None
    needed = msg.split("Permissions needed:", 1)[1].strip().rstrip(".")

    # The role each permission needs, MEASURED against Grafana 13.0.3 rather than
    # reasoned about. This table exists because the reasoning was wrong: the first
    # version of this function announced that `datasources:read` meant org Admin
    # and that an Editor could not discover datasources. Both false — a **Viewer**
    # lists datasources fine. Only the write permissions actually gate anything.
    # tests/test_roles_live.py pins every row against a real server.
    if needed.startswith("datasources:read"):
        return (
            f"your token lacks {needed}, which is what listing datasources requires. "
            f"That is unusual — on Grafana OSS every basic role from Viewer up has it "
            f"by default — so this token is probably restricted by custom RBAC. "
            f"Without it `logs sources` cannot work: you can query a datasource whose "
            f"uid you already know, but not discover which exist."
        )
    if needed.startswith("alert.rules:"):
        return (
            f"your token lacks {needed}. Creating or editing alert rules needs the "
            f"**Editor** role (Viewer cannot), and the permission is scoped PER "
            f"FOLDER — so a token that can write rules in one folder still cannot in "
            f"another. `grafana-cli server doctor` lists the folders yours can write to."
        )
    if needed.startswith("alert.notifications"):
        return (
            f"your token lacks {needed}. Managing contact points and notification "
            f"policies needs the **Editor** role; a Viewer can read them but not "
            f"change them."
        )
    if needed.startswith("dashboards:"):
        return (
            f"your token lacks {needed}. Creating or editing dashboards needs the "
            f"**Editor** role, and it is scoped per folder."
        )
    return f"your token lacks the {needed} permission."
