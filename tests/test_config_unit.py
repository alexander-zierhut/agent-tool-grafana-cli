"""Config, profiles, settings and the per-profile context — hermetic."""

from __future__ import annotations

import pytest
from agentcli.errors import ConfigError

from grafanacli.config import DEFAULT_LIMIT, DEFAULT_SINCE, Config, Profile
from grafanacli.spec import SPEC, token_url


# ---- the token URL (requirement: show the user where to get a token) ----

def test_token_url_points_at_service_accounts_not_api_keys():
    """API keys are deprecated since Grafana 9.1 and a token minted there cannot
    be scoped or rotated the same way, so `/org/apikeys` is the wrong place to
    send someone even though it still exists."""
    assert token_url("https://grafana.example.com") == "https://grafana.example.com/org/serviceaccounts"
    assert token_url("https://grafana.example.com/") == "https://grafana.example.com/org/serviceaccounts"


# ---- profiles / env --------------------------------------------------

def test_api_root_is_unversioned():
    assert Profile(name="d", base_url="https://g.example.com/").api_root() == "https://g.example.com/api"


def test_env_url_synthesises_a_profile(monkeypatch):
    """GRAFANA_URL + GRAFANA_TOKEN must be enough with no config file at all --
    that is exactly how this runs inside CI."""
    monkeypatch.setenv("GRAFANA_URL", "https://g.example.com")
    assert Config().resolve().base_url == "https://g.example.com"


def test_our_url_var_beats_the_ecosystem_one(monkeypatch):
    monkeypatch.setenv("GRAFANA_URL", "https://ecosystem.example.com")
    monkeypatch.setenv("GRAFANACLI_URL", "https://ours.example.com")
    assert Config().resolve().base_url == "https://ours.example.com"


def test_env_overrides_a_saved_profile(monkeypatch):
    cfg = Config()
    cfg.upsert_profile(Profile(name="default", base_url="https://saved.example.com"))
    monkeypatch.setenv("GRAFANA_URL", "https://env.example.com")
    assert cfg.resolve().base_url == "https://env.example.com"


def test_env_url_override_keeps_the_rest_of_the_profile(monkeypatch):
    """Pointing at a different host must not silently drop the org assertion --
    that would turn an override into a wrong-org query."""
    cfg = Config()
    cfg.upsert_profile(Profile(name="default", base_url="https://a.example.com", org_id=6, org_name="Sales"))
    monkeypatch.setenv("GRAFANA_URL", "https://b.example.com")
    prof = cfg.resolve()
    assert (prof.base_url, prof.org_id, prof.org_name) == ("https://b.example.com", 6, "Sales")


def test_no_profile_and_no_env_is_a_config_error():
    with pytest.raises(ConfigError) as e:
        Config().resolve()
    assert "auth login" in str(e.value), "the error must say how to fix it"


def test_roundtrip_persists_everything():
    cfg = Config()
    cfg.upsert_profile(Profile(name="default", base_url="https://g.example.com", org_id=1, org_name="Main"))
    cfg.upsert_profile(Profile(name="sales", base_url="https://g.example.com", org_id=6, org_name="Sales"),
                       make_current=False)
    cfg.default_since = "30m"
    cfg.default_limit = 500
    cfg.default_format = "table"
    cfg.save()

    again = Config.load()
    assert again.default_since == "30m"
    assert again.default_limit == 500
    assert again.default_format == "table"
    assert again.profiles["sales"].org_id == 6
    assert again.profiles["sales"].org_name == "Sales"
    assert again.current_profile == "default"


def test_defaults_are_sane_with_no_config_file():
    """Every setting must have a default; nothing may be half-configured."""
    cfg = Config()
    assert cfg.default_since == DEFAULT_SINCE == "1h"
    assert cfg.default_limit == DEFAULT_LIMIT == 100
    assert cfg.default_format is None, "None = never chosen -> ask once, then json"


def test_malformed_config_is_a_clean_error():
    SPEC.config_file().parent.mkdir(parents=True, exist_ok=True)
    SPEC.config_file().write_text("{not json")
    with pytest.raises(ConfigError):
        Config.load()


# ---- orgs ------------------------------------------------------------

def test_org_id_from_env(monkeypatch):
    monkeypatch.setenv("GRAFANA_URL", "https://g.example.com")
    monkeypatch.setenv("GRAFANA_ORG_ID", "6")
    assert Config().resolve().org_id == 6


def test_our_org_var_beats_the_ecosystem_one(monkeypatch):
    monkeypatch.setenv("GRAFANA_URL", "https://g.example.com")
    monkeypatch.setenv("GRAFANA_ORG_ID", "6")
    monkeypatch.setenv("GRAFANACLI_ORG_ID", "1")
    assert Config().resolve().org_id == 1


def test_env_org_overrides_the_profile(monkeypatch):
    cfg = Config()
    cfg.upsert_profile(Profile(name="default", base_url="https://g.example.com", org_id=1))
    monkeypatch.setenv("GRAFANACLI_ORG_ID", "6")
    assert cfg.resolve().org_id == 6


def test_garbage_org_env_drops_the_assertion_rather_than_bricking(monkeypatch):
    """The org header is an assertion, not a capability. A stale env var must not
    make every command unrunnable -- dropping the assertion degrades gracefully,
    refusing to start does not."""
    monkeypatch.setenv("GRAFANA_URL", "https://g.example.com")
    monkeypatch.setenv("GRAFANA_ORG_ID", "not-a-number")
    assert Config().resolve().org_id is None


# ---- THE per-profile context ----------------------------------------

def test_context_is_scoped_to_the_active_profile():
    """The bug this design prevents: datasource UIDs are per-org (the same Loki is
    `P1A2B…` in org 1 and `ae9zq…` in org 6). A single global sticky default would
    hand Grafana a uid that does not exist the moment you switch profile, and
    Grafana answers with a 404 that blames the datasource, not the config."""
    cfg = Config()
    cfg.upsert_profile(Profile(name="default", base_url="https://g.example.com"))
    cfg.set_context({"datasource": "P1A2B3C4D5E6F7890"})

    cfg.upsert_profile(Profile(name="sales", base_url="https://g.example.com"))
    assert cfg.context == {}, "a fresh profile must not inherit another org's uid"

    cfg.set_context({"datasource": "ae9zq33kx01mbd"})
    assert cfg.context == {"datasource": "ae9zq33kx01mbd"}

    cfg.current_profile = "default"
    assert cfg.context == {"datasource": "P1A2B3C4D5E6F7890"}, "switching back restores the original"


def test_context_survives_a_roundtrip_per_profile():
    cfg = Config()
    cfg.upsert_profile(Profile(name="default", base_url="https://g.example.com"))
    cfg.set_context({"datasource": "aaa"})
    cfg.upsert_profile(Profile(name="sales", base_url="https://g.example.com"))
    cfg.set_context({"datasource": "bbb"})
    cfg.save()

    again = Config.load()
    again.current_profile = "default"
    assert again.context == {"datasource": "aaa"}
    again.current_profile = "sales"
    assert again.context == {"datasource": "bbb"}


def test_context_follows_the_profile_env_var(monkeypatch):
    """`--profile` is exported as GRAFANACLI_PROFILE by the root callback, so the
    context property must read through it -- otherwise `-p sales` would use the
    sales token with the default profile's datasource."""
    cfg = Config()
    cfg.upsert_profile(Profile(name="default", base_url="https://g.example.com"))
    cfg.set_context({"datasource": "aaa"})
    cfg.contexts["sales"] = {"datasource": "bbb"}

    monkeypatch.setenv("GRAFANACLI_PROFILE", "sales")
    assert cfg.context == {"datasource": "bbb"}


def test_clear_context_only_clears_the_active_profile():
    cfg = Config()
    cfg.contexts = {"default": {"datasource": "aaa"}, "sales": {"datasource": "bbb"}}
    cfg.current_profile = "default"
    cfg.clear_context()
    assert cfg.contexts == {"sales": {"datasource": "bbb"}}


def test_context_of_an_unknown_profile_is_empty_not_an_error():
    assert Config().context == {}
