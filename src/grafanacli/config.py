"""Non-secret configuration: connection profiles and settings.

Config is a plain JSON file (``~/.config/grafana-cli/config.json`` by default). It
never contains the API token — that lives in the OS keyring (see :mod:`spec`).

Environment overrides:

* ``GRAFANA_URL`` / ``GRAFANACLI_URL`` — the server URL (``GRAFANACLI_`` wins).
* ``GRAFANACLI_PROFILE`` — selects the active profile.
* ``GRAFANA_TOKEN`` / ``GRAFANACLI_TOKEN`` — the token directly.
* ``GRAFANACLI_CONFIG_DIR`` / ``XDG_CONFIG_HOME`` — relocate this directory.

Every setting below has a **sane default** or is **asked once on first run** —
there is no silent half-configured state.

**On organisations — the design decision this file exists to encode.**
Grafana service-account tokens are hard-scoped to exactly one org (verified live:
a token from org 1 sent with ``X-Grafana-Org-Id: 6`` gets a 401
``api-key.organization-mismatch``, and the reverse too). There is no way to widen
one. So "work with multiple organisations" cannot mean "switch org on a flag" —
it means **one profile per org**, each with its own URL, token and org id::

    graf auth login                      # -> profile "default", org 1
    graf auth login --profile sales      # -> profile "sales",   org 6
    graf -p sales logs sources

The consequence that bites, and the reason ``context`` is keyed by profile below:
**datasource UIDs are per-org.** The same Loki is uid ``P1A2B…`` in org 1 and
``ae9zq…`` in org 6. A single global sticky ``--datasource`` default would resolve
to a UID that does not exist the moment you switch profile, and Grafana answers
that with a 404 that blames the datasource rather than the config. Scoping the
context by profile makes that unrepresentable.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentcli.errors import ConfigError

from .spec import SPEC

DEFAULT_PROFILE = "default"

#: How far back log/metric queries look when ``--since`` is omitted. An hour is
#: the "I just deployed, what happened" window this tool is built for.
DEFAULT_SINCE = "1h"

#: Default cap on returned log lines. Loki's own default is 100; the live
#: instance carries ~87 units across ~21 hosts, so an uncapped query is a
#: context-window accident waiting to happen.
DEFAULT_LIMIT = 100

#: Datasource types this CLI knows how to pull **logs** from. Used by
#: `logs sources` to filter the datasource list down to the log-capable ones.
#: Only `loki` is implemented today; the rest are recognised so that
#: `logs sources` can honestly say "this exists but I cannot query it" rather
#: than pretend the datasource is not there.
LOG_DATASOURCE_TYPES = {
    "loki": "supported",
    "elasticsearch": "recognised",
    "opensearch": "recognised",
    "cloudwatch": "recognised",
    "grafana-splunk-datasource": "recognised",
    "grafana-opensearch-datasource": "recognised",
}

#: Datasource types this CLI knows how to pull **metrics** from.
METRIC_DATASOURCE_TYPES = {
    "prometheus": "supported",   # also Mimir/Thanos/Cortex -- all speak PromQL
    "graphite": "recognised",
    "influxdb": "recognised",
}


def config_dir() -> Path:
    return SPEC.config_dir()


def config_path() -> Path:
    return SPEC.config_file()


@dataclass
class Profile:
    """One (server, org, token) triple. See the module docstring on why org
    lives here rather than on a flag."""

    name: str
    base_url: str
    org_id: int | None = None      # informational + asserted on every request
    org_name: str | None = None    # informational; for `auth status` output
    username: str | None = None    # informational; the login of the API identity
    verify_ssl: bool = True

    def api_root(self) -> str:
        # Grafana's HTTP API is unversioned and mounted at /api. Note that the
        # useful sub-APIs are NOT uniform underneath it: alerting alone spans
        # /api/v1/provisioning/*, /api/alertmanager/*, /api/ruler/* and
        # /api/prometheus/*, each a different upstream project's shape.
        return self.base_url.rstrip("/") + "/api"


@dataclass
class Config:
    current_profile: str = DEFAULT_PROFILE
    profiles: dict[str, Profile] = field(default_factory=dict)

    # --- settings (sane default, or asked on first run) ---
    default_format: str | None = None       # None = never chosen -> ask once
    default_since: str = DEFAULT_SINCE
    default_limit: int = DEFAULT_LIMIT
    claude_prompted: bool = False

    # Sticky session context, **keyed by profile name** (see module docstring):
    #   {"default": {"datasource": "P1A2B…"}, "sales": {"datasource": "ae9zq…"}}
    # This diverges from drone/openproject, where `context` is a single flat dict,
    # and the divergence is deliberate: their identifiers (a repo slug, a project
    # id) are stable across profiles. Grafana's datasource UIDs are not.
    contexts: dict[str, dict] = field(default_factory=dict)

    # ---- persistence -------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
            profiles = {
                name: Profile(
                    name=name,
                    base_url=p["base_url"],
                    org_id=p.get("org_id"),
                    org_name=p.get("org_name"),
                    username=p.get("username"),
                    verify_ssl=p.get("verify_ssl", True),
                )
                for name, p in raw.get("profiles", {}).items()
            }
            return cls(
                current_profile=raw.get("current_profile", DEFAULT_PROFILE),
                profiles=profiles,
                default_format=raw.get("default_format"),
                default_since=raw.get("default_since") or DEFAULT_SINCE,
                default_limit=int(raw.get("default_limit") or DEFAULT_LIMIT),
                claude_prompted=bool(raw.get("claude_prompted", False)),
                contexts=raw.get("contexts") or {},
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ConfigError(f"malformed config at {path}: {exc}") from exc

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "current_profile": self.current_profile,
            "default_format": self.default_format,
            "default_since": self.default_since,
            "default_limit": self.default_limit,
            "claude_prompted": self.claude_prompted,
            "contexts": self.contexts,
            "profiles": {
                name: {
                    "base_url": p.base_url,
                    "org_id": p.org_id,
                    "org_name": p.org_name,
                    "username": p.username,
                    "verify_ssl": p.verify_ssl,
                }
                for name, p in self.profiles.items()
            },
        }
        path.write_text(json.dumps(data, indent=2) + "\n")

    # ---- resolution --------------------------------------------------
    def active_profile_name(self) -> str:
        return SPEC.getenv("PROFILE") or self.current_profile

    def _env_url(self) -> str | None:
        """GRAFANACLI_URL wins over the ecosystem's GRAFANA_URL."""
        return SPEC.getenv("URL") or os.environ.get("GRAFANA_URL")

    def _env_org(self) -> int | None:
        raw = SPEC.getenv("ORG_ID") or os.environ.get("GRAFANA_ORG_ID")
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            # A stale/garbage env var must not brick every command; the org is an
            # assertion, and dropping the assertion is better than refusing to run.
            return None

    def resolve(self) -> Profile:
        """The effective profile, applying env overrides.

        A profile can be synthesised entirely from the environment with no config
        file on disk, so ``GRAFANA_URL`` + ``GRAFANA_TOKEN`` are enough to run
        headless — which is exactly how this gets used inside CI.
        """
        name = self.active_profile_name()
        env_url = self._env_url()
        env_org = self._env_org()

        prof = self.profiles.get(name)
        if prof is None:
            if env_url:
                return Profile(name=name, base_url=env_url, org_id=env_org)
            raise ConfigError(
                f"no profile '{name}' configured. Run `graf auth login` "
                f"or set GRAFANA_URL + GRAFANA_TOKEN."
            )
        return Profile(
            name=prof.name,
            base_url=env_url or prof.base_url,
            org_id=env_org if env_org is not None else prof.org_id,
            org_name=prof.org_name,
            username=prof.username,
            verify_ssl=prof.verify_ssl,
        )

    def upsert_profile(self, prof: Profile, make_current: bool = True) -> None:
        self.profiles[prof.name] = prof
        if make_current:
            self.current_profile = prof.name

    # ---- sticky context (per profile) --------------------------------
    @property
    def context(self) -> dict:
        """The active profile's sticky defaults.

        A property rather than a field so that callers read the *current*
        profile's context even after `--profile` has changed underneath them.
        """
        return self.contexts.get(self.active_profile_name()) or {}

    def set_context(self, values: dict) -> None:
        self.contexts[self.active_profile_name()] = values

    def clear_context(self) -> None:
        self.contexts.pop(self.active_profile_name(), None)
