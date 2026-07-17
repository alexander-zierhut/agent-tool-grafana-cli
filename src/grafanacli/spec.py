"""This tool's identity, and the shared services derived from it.

Everything tool-specific about the shared chassis (`agentcli`) is these strings.
The config directory, the keyring service and every env var follow from them.

On the env-var namespace — the same split as Drone, for the same reason:

* ``GRAFANA_URL`` / ``GRAFANA_TOKEN`` are what the ecosystem already exports
  (Grafana's own tooling, Terraform's provider, most CI recipes). We honour them.
* ``GRAFANACLI_*`` is for everything *we* invent (``GRAFANACLI_CONFIG_DIR``,
  ``GRAFANACLI_FORMAT``, …) and takes precedence for the token.
"""

from __future__ import annotations

from agentcli import AppSpec, Credentials

SPEC = AppSpec(
    name="grafana-cli",
    env_prefix="GRAFANACLI",
    # The ecosystem's variable. Honoured after GRAFANACLI_TOKEN.
    token_env_aliases=("GRAFANA_TOKEN",),
)

credentials = Credentials(SPEC)


def token_url(server: str) -> str:
    """Where a human gets an API token, given a server URL.

    Service accounts live at ``<server>/org/serviceaccounts``. Deriving this from
    what the operator just typed — rather than telling them to "go find it" — is
    the difference between a 10-second login and a support question.

    Note this is deliberately the **service account** page and not the legacy
    ``/org/apikeys``: API keys are deprecated since Grafana 9.1, and a token
    minted there cannot be scoped or rotated the same way.
    """
    return server.rstrip("/") + "/org/serviceaccounts"
