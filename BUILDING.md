# Building this tool — the shared chassis and the standard workflow

Everything needed to build `agent-tool-grafana-cli` the way the other two were built.
Read this, then `spike/VERIFIED_FINDINGS.md` (live API truth), then work §7 as a checklist.

**The full standard is `/workspace/Development/zierhut-it/agent-tools/_shared/BLUEPRINT.md` (946 lines).**
This file is the working subset plus what is Grafana-specific.

**The two reference implementations — read them, copy their shape:**
- `/workspace/Development/zierhut-it/agent-tools/drone/` — the freshest, closest sibling
- `/workspace/Development/zierhut-it/agent-tools/openproject/` — the original

---

## 1. What the shared chassis gives you

`agent-tool-shared-cli` (import name **`agentcli`**), on PyPI at **0.1.1**, repo
`/workspace/Development/zierhut-it/agent-tools/_shared/`. Already a dependency in `pyproject.toml`.

```python
from agentcli import (
    AppSpec, Credentials,          # identity + token storage
    Emitter, OutputFormat,         # ALL output goes through this
    print_error,                   # errors -> stderr as JSON
    OpError, ApiError, AuthError, ConfigError,
    ConflictError, NotFoundError, ValidationError, DryRun,
)
```

### `AppSpec` — the only tool-specific part of the chassis
```python
SPEC = AppSpec(
    name="grafana-cli",             # -> ~/.config/grafana-cli/ + keyring service
    env_prefix="GRAFANACLI",        # -> GRAFANACLI_TOKEN, GRAFANACLI_CONFIG_DIR, …
    token_env_aliases=("GRAFANA_TOKEN",),   # the ecosystem's name, honoured AFTER ours
)
SPEC.config_dir()          # FUNCTION, not a constant — this is what makes tests hermetic
SPEC.config_file()         # ~/.config/grafana-cli/config.json
SPEC.credentials_file()
SPEC.env("URL")            # -> "GRAFANACLI_URL"
SPEC.getenv("URL")         # -> os.environ.get("GRAFANACLI_URL")
SPEC.token_env_names()     # -> ("GRAFANACLI_TOKEN", "GRAFANA_TOKEN")
```
`token_env_aliases` exists **because of Drone** — tool #2 needed `DRONE_TOKEN`. Grafana needs
`GRAFANA_TOKEN` for the same reason. This is the seam working as designed.

### `Credentials`
```python
credentials = Credentials(SPEC)          # module-level instance in spec.py
credentials.get_token(profile)           # env > keyring > 0600 file
credentials.store_token(profile, token)  # -> "keyring" | "file"
credentials.delete_token(profile)
credentials.backend_name()               # names WHICH env var/backend actually spoke
```
**Precedence is env > keyring > file and must not be inverted** (CI depends on it). But an exported
`GRAFANA_TOKEN` therefore silently overrides a keyring login — so `auth status` must always name the
backend in use. Visibility, not inversion.

### `Emitter` — every payload, no exceptions
```python
obj.emitter.emit(data, columns=["uid", "type", "name"])   # json|table|markdown|csv + --fields
obj.emitter.stream_json(iterable)                          # NDJSON
obj.emitter.message("human note")                          # TABLE MODE ONLY — allowlisted
```
Never `print()` a payload. Never build JSON by hand. `OutputFormat.coerce("md")` → markdown.

### Exit codes — published API across the whole family
`0` ok (incl. a successful `--dry-run`) · `1` generic · `2` **reserved for Click/Typer, never allocate** ·
`3` config · `4` auth · `5` not-found · `6` conflict · `7` validation · `130` SIGINT.
Extend in `errors.py` from 8 upward, **only for a condition you have OBSERVED**.
Never renumber. Leave holes: Drone reserved 6 rather than recycling it.

---

## 2. Module layout (copy from `drone/src/dronecli/`)

```
src/grafanacli/
  __init__.py      __version__ = "0.1.0"   — the SINGLE source (pyproject reads it dynamically)
  __main__.py      `python -m grafanacli`  — REQUIRED; the test harness shells it. Forgetting it broke drone.
  spec.py          SPEC = AppSpec(...) + credentials instance + token_url(server)
  errors.py        re-export the shared taxonomy + Grafana-specific codes (8+)
  config.py        Profile/Config, settings, env precedence
  appctx.py        AppContext: DI container. NOT named context.py — that is the sticky-defaults feature
  client.py        httpx wrapper: auth, retry matrix, pagination, dry-run interception, error mapping
  <domain>.py      pure logic (drone has builds.py) — the testable heart
  commands/
    _shared.py     ctx_obj(), need_*() resolvers
    guide.py       the built-in manual  <- NON-NEGOTIABLE, see §5
    auth.py settings.py context.py install.py raw.py
    <domain groups>
```

**Layering rule:** `output.py`/`errors.py` (in agentcli) import nothing from the tool.
`client.py` must not import command modules. Pure logic goes in `<domain>.py`, not in commands —
that is what makes 500 hermetic tests possible.

---

## 3. The chassis files to copy near-verbatim from drone

| file | change |
|---|---|
| `cli.py` | `_pop_globals`, the error funnel, `_context_default_map`, the first-run gate. Swap names. |
| `appctx.py` | format precedence + first-run prompts. Swap env names. |
| `commands/_shared.py` | `ctx_obj` + resolvers |
| `commands/context.py` | `KNOWN_KEYS` becomes Grafana's (see §6) |
| `commands/install.py` | SKILL.md is fresh; the machinery is verbatim |
| `commands/raw.py`, `settings.py` | near-verbatim |
| `scripts/build_binary.py` | **already has the Windows fix** — `write_text(..., encoding="utf-8")` + ASCII launcher |
| `scripts/gen_docs.py` | 2 strings change |
| `.github/workflows/ci.yml` | unit matrix + build + docs-drift |
| `release.yml` | **already written here**, with both release scars fixed |

---

## 4. The reserved global namespace — get this right first

`cli.py::_pop_globals` strips these from **anywhere** on the line:
```python
_FORMAT_FLAGS = ("--format", "-f", "--output", "-o")
_FIELDS_FLAGS = ("--fields", "--columns")
_BOOL_FLAGS   = ("--dry-run", "--stream", "--no-context")
```
**No command may declare an option with those names** — it could never receive one.
This shipped as a real silent-wrong-file bug in OpenProject for four releases
(`attach download --output f.pdf` wrote to CWD, exit 0).

Copy `drone/tests/test_globals_unit.py`: it walks the whole tree and fails on a collision.
**Reserve ONLY the popped flags.** `--version/-V`, `--profile/-p`, `--no-color` are ordinary root
options that Click lets a subcommand shadow — reserving them flags four working commands as broken.

For Grafana specifically: `logs --output` and `dashboard export --output` are the obvious traps.
**Standardise file destinations on `--out PATH`.**

---

## 5. The command surface every tool ships

- **`guide`** — the built-in manual. An agent with only this binary and no context runs it first, so
  it must be **impossible** for it to fail on config/auth or block on a prompt. Verify with:
  `env -i PATH=/usr/bin:/bin HOME=/nonexistent ./graf guide` → exit 0.
  Structure: OVERVIEW (what it is · output contract · exit codes · auth · THE key concept · gotchas ·
  discover · TOPICS list) + `TOPICS: dict[str, str]`. Include a **`gotchas`** topic — an agent reaches
  for that name first (drone shipped SKILL.md pointing at a `gotchas` topic that didn't exist).
- **`auth login`** — interactive: ask for the URL, then **show where to get the token**, derived from
  what they just typed: `<server>/org/serviceaccounts`. Verify the token before persisting.
- **`auth status`** — must name WHICH token/backend is in use (the `GRAFANA_TOKEN`-beats-keyring trap).
- **`settings`** — every setting has a sane default or is asked once on first run.
- **`context`** — sticky defaults. `KNOWN_KEYS` must match real option names (test it).
- **`install claude`** — writes `~/.claude/skills/<name>/SKILL.md`. Anchor every trigger word to the
  product noun ("Grafana", "a Grafana dashboard", "Loki logs"); bare "logs"/"dashboard" over-fires.
- **`raw`** — the escape hatch.
- **`server doctor`** (Grafana: chain `/api/health` → `/api/user` → `/api/access-control/user/permissions`
  → datasource list). **Doctor must never raise; returning the report IS its job.** Watch the sibling
  trap: `NotFoundError` and `ApiError` are SIBLINGS, so `except ApiError` ladders look exhaustive and
  are not. End every probe in `except OpError`.

---

## 6. Grafana-specific decisions

- **Command name: `graf`.** NOT `grafana-cli` (the official binary shipped with every Grafana server)
  and NOT `grafanactl` (Grafana's own dashboards-as-code tool). Same call as refusing `op`/`drone`.
  Dist stays `agent-tool-grafana-cli`.
- **Env:** read `GRAFANA_URL`/`GRAFANA_TOKEN` (ecosystem) + `GRAFANACLI_*` (ours, higher precedence).
- **Context `KNOWN_KEYS`:** likely `["datasource", "org"]` — maybe `["datasource", "org", "since"]`.
  Decide against real options; the test enforces it.
- **The killer feature = discovery.** Per the family principle (*derive the answer the API refuses to
  give*): Grafana will not tell you "what can I get logs from?" anywhere. You must enumerate
  datasources → filter to log-capable types (`loki`, `elasticsearch`, `cloudwatch`, opensearch,
  splunk) → hit each one's label API. **`graf logs sources`** does it in one command.
- **Logs are TEXT, not JSON** — the one carve-out from the output contract. Document it on the guide's
  first screen, exactly as drone does for `log view`.
- Use the **datasource proxy** for Loki, not `/api/ds/query` (dataframes are far more work). See
  `spike/VERIFIED_FINDINGS.md`.

---

## 7. Build order (proven twice)

0. **Spike first.** DONE — see `spike/VERIFIED_FINDINGS.md`. *Reading source is not observation:* for
   Drone, doc-research produced two confident, wrong conclusions that a half-day live probe killed.
1. Chassis: `spec/errors/config/client/appctx/cli` + `__main__.py`. Reserved-flag test immediately.
2. Pure logic module + its hermetic tests (drone: 33 tests before a single command).
3. The killer feature early — it is the product, not a nice-to-have.
4. Read surface, then write surface (`--dry-run` from day one).
5. `guide` + `install claude` + docs + CI.
6. **Wire every group into `cli.py` and verify each resolves** — drone shipped 5 fully-implemented
   groups that were never registered, and one missing import broke the whole suite.
7. Regenerate docs, run the full suite, verify against the live instance, tag.

**Fan out with agents for the bulk command modules** (it worked well for drone: 5 implementers + 5
adversarial reviewers), but **the orchestrator owns `cli.py`, the chassis, and the review**. Give each
agent: the reference module, the conventions, the VERIFIED_FINDINGS, and explicit file ownership.

---

## 8. Testing

- **The contributor promise, stated FIRST in the README:** `pip install -e '.[test]' && pytest` is
  green on a clean checkout — no Docker, no server, no token, ~2s. Never lead docs with the stack.
- Hermetic tests use a **hand-rolled fake client**, not httpx MockTransport, and never simulate API
  semantics (the API is wrong in ways you would encode wrongly — that is what the spike is for).
- `conftest.py`: `_reachable()` probes once at collection; absent config → **skip, not fail**, with an
  actionable message naming `GRAFANA_URL` + `GRAFANA_TOKEN`.
- `make test-unit` = `pytest -m "not integration"` — **the marker, never a file list** (OpenProject's
  ran 30 of 144 while claiming all).
- Live instance: Grafana **13.0.3** with a Loki + a Prometheus/Mimir datasource. URL, token and
  datasource UIDs live in `.env` and the gitignored `spike/local-instance.md` — **never in this repo**.

## 9. Hard-won rules worth re-reading

- **Never state a count you have not counted**, and name the basis — counts rot on every commit.
- **`.gitignore` before the first secret-adjacent file**, and verify staged files before every push.
- **Don't leak observed status into the exit code.** "The build failed" ≠ "the CLI failed".
  Gate it behind an explicit `--exit-code` in a band far from the error codes.
- Generated `.py` files: `write_text(..., encoding="utf-8")` + ASCII content, or Windows dies.
- Bash: `UID` is readonly. Use another name.
- A doc that names a command must be tested against the real tree (`test_guide_unit.py`).
