# agent-tool-grafana-cli

Agent-ready CLI for **Grafana**, from a developer's perspective: *find out what
you can even get logs from, then get them* — and set up an alert so you hear
about it next time.

[![PyPI](https://img.shields.io/pypi/v/agent-tool-grafana-cli)](https://pypi.org/project/agent-tool-grafana-cli/)
[![CI](https://github.com/alexander-zierhut/agent-tool-grafana-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/alexander-zierhut/agent-tool-grafana-cli/actions/workflows/ci.yml)
![Python](https://img.shields.io/pypi/pyversions/agent-tool-grafana-cli)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Agent ready](https://img.shields.io/badge/agent-ready-8A2BE2)

```bash
pipx install agent-tool-grafana-cli
grafana-cli guide
```

**Keywords:** Grafana CLI, Grafana command line, Grafana API client, Loki logs,
LogQL, observability CLI, SRE tooling, alerting, datasources, dashboards, AI agent
tool, LLM tooling, Claude, DevOps automation.

## Contributing takes two commands

```bash
pip install -e '.[test]' && pytest
```

Green on a clean checkout. **No Docker, no Grafana, no token, no network** — the
hermetic tier (268 tests) runs in about two seconds. Live tests skip themselves
unless you point them at a server; a missing token is the normal case, not a
failure.

Want the other 53? Boot a throwaway Grafana + Loki + Prometheus:

```bash
make test          # boots the stack, seeds it, runs everything
```

or by hand:

```bash
docker compose up -d --wait
eval "$(./scripts/bootstrap_test_stack.sh --export)"
pytest
```

The stack is what CI runs, with **no secrets** — so a fork's pull request gets
exactly the same coverage as `main`. It seeds two orgs, one service account per
role, a Loki with deliberate label cardinality, a datasource whose backend is
dead (to prove exit 8 against a real 502), and an alert rule that fires into a
receiver that cannot deliver. That last one is not contrived: **a fresh Grafana
org ships a default contact point named `empty` with zero integrations**, so out
of the box every alert it raises notifies nobody.

Tests that write are gated behind `GRAFANA_ALLOW_WRITES=1`, which only the
bootstrap script sets. Point the suite at a real Grafana and it physically cannot
reach the write tier.

## What it's for

You deployed. Something feels wrong. Fifteen seconds:

```bash
grafana-cli logs sources              # what can I even get logs from?
grafana-cli scan --since 15m          # is it broken? what should I look at first?
grafana-cli logs similar "<a line>"   # has this happened before, or elsewhere?
grafana-cli alert create --title "..." -q '<query>' --folder <uid>
                               # ...and it tells you whether that alert would
                               # actually reach a human
```

It pairs with the siblings: `drone-cli wait --commit HEAD && grafana-cli scan --since 10m`
answers *"my commit built — is it healthy in production?"*

## The command surface

Everything is discoverable from the binary — `grafana-cli --help`, then
`grafana-cli guide` for the playbook and `grafana-cli <group> --help` for any
group. The top level:

```text
 Usage: grafana-cli [OPTIONS] COMMAND [ARGS]...

 Agent-friendly CLI for Grafana: find what you can get logs from, read them, work
 out what is wrong, and set up an alert so you hear about it next time.

 Output is JSON on stdout by default (errors are JSON on stderr with a non-zero
 exit code); add `-o table` or trim with `--fields label,values`. Start with
 `grafana-cli logs sources` — you cannot query what you cannot name.

 New here / no context? Run `grafana-cli guide` for the full playbook.

╭─ Commands ───────────────────────────────────────────────────────────────────────╮
│ guide       Built-in operating guide — how to use this CLI without external docs.│
│ scan        Is this healthy? Find errors, panics and deprecations in one pass.   │
│ logs        Logs: discover sources, query, and find similar problems.            │
│ metrics     Metrics: PromQL against Prometheus/Mimir.                            │
│ dashboard   Dashboards: find, read, create.                                      │
│ alert       Alert rules — including `alert route`: will it reach you?            │
│ notify      Contact points, notification policies, silences.                     │
│ datasource  Datasources: list, inspect, health-check.                            │
│ org         Organisations — and why a token only ever sees one.                  │
│ auth        Log in, log out, inspect credentials.                                │
│ server      Health, version — and `server doctor`.                               │
│ raw         Escape hatch: call any API endpoint directly.                        │
│ settings    View & change CLI settings.                                          │
│ context     Sticky session defaults (datasource, etc.), per profile.            │
│ install     Integrate with other tools (e.g. `install claude`).                 │
╰──────────────────────────────────────────────────────────────────────────────────╯
```

Global options — `-o/--output json|table|markdown|csv`, `--fields`, `--dry-run`,
`--stream`, `--no-context` — work anywhere on the line. Full reference:
[docs/COMMANDS.md](docs/COMMANDS.md).

## The killer feature: discovery

Grafana will not tell you, anywhere in the UI or the API, **what you can get logs
from**. You are expected to already know. Answering it means enumerating
datasources, filtering to the log-capable types, hitting each one's label API, and
then counting the values behind every label.

That last step is the point:

```
$ grafana-cli logs sources
  systemd_unit    91 values   useful=True
  hostname        21 values   useful=True
  job              1 values   useful=False
  service_name     1 values   useful=False
```

A label with one value cannot narrow anything down. Listing it as a filter you
"can use" is noise dressed up as help — so `sources` counts rather than lists.

## The second one: will this alert actually reach me?

Grafana gives you the rule, the notification policy tree, and the contact points.
It joins none of them. So an alert can fire forever into a receiver with zero
integrations, and every screen shows it firing normally.

```bash
grafana-cli alert route <uid>
```

walks rule → labels → policy tree → receiver → integrations and says, in words,
that nobody will be notified. `grafana-cli notify check` does it for every rule at once.

## Multiple organisations

A Grafana service-account token is **hard-scoped to one org** — there is no header
or flag that widens it. So multi-org is one profile per org:

```bash
grafana-cli auth login                     # org A -> profile "default"
grafana-cli auth login --profile sales     # org B -> profile "sales"
grafana-cli -p sales logs sources
```

Datasource uids differ per org, so sticky defaults (`grafana-cli context`) are stored per
profile. Asking for the wrong org is exit **9**, not a generic auth error — because
re-running `auth login` with the same token cannot fix it.

## The contract

- **stdout is JSON.** Errors are JSON on stderr with a non-zero exit code.
  (One carve-out: `logs query --raw` prints log text. Logs are prose.)
- **Exit codes are API:** `0` ok · `1` generic · `3` config · `4` auth ·
  `5` not-found · `6` conflict · `7` validation · `8` datasource-unreachable ·
  `9` wrong-org · `130` interrupted. Codes 0–7 mean the same across the family.
- **Findings are not failures.** `scan` exits 0 on a broken project, because it
  succeeded at scanning. Opt into exit 20 with `--exit-code`.
- `-o table|markdown|csv`, `--fields a,b`, `--stream` (NDJSON) work anywhere on
  the line. **File destinations are `--out`** — `--output` is a reserved format flag.
- `--dry-run` previews any write without sending it.
- `grafana-cli guide` is the built-in manual; it works with no config, no token and no
  network, because that is exactly when you need it.

## Requirements

A **service account token** (`<your-grafana>/org/serviceaccounts`). Which role you
need depends only on whether you intend to write — measured against Grafana 13.0.3
and pinned by `tests/test_roles_live.py`:

| role | discover & read logs/metrics | create dashboards, alerts, contact points |
|---|---|---|
| **Viewer** | ✅ everything | ❌ 403 |
| **Editor** | ✅ everything | ✅ everything |
| Admin | same as Editor | same as Editor |

So: **Viewer for read-only** (`scan`, `logs`, `metrics`, `alert route`), **Editor**
to create anything. **Admin buys this CLI nothing** — don't hand out more than you
need. `grafana-cli server doctor` reports what your token can actually do, with scopes.

Note that alert-rule and dashboard permissions are **scoped per folder**, so an
Editor token can write in one folder and be refused in another. Doctor lists which.

Works against Grafana with **Loki** (logs) and **Prometheus/Mimir/Thanos/Cortex**
(metrics). Verified against Grafana 13.0.3.

## Part of the family

Built on **[agent-tool-shared-cli](https://github.com/alexander-zierhut/agent-tool-shared-cli)** —
the chassis every tool in this family shares: JSON on stdout, JSON errors on
stderr, a stable cross-tool exit-code contract, `--dry-run`, four output formats,
and a built-in `guide` so an agent can learn each tool from the tool itself.

| Tool | Install | For |
| --- | --- | --- |
| [**drone-cli**](https://github.com/alexander-zierhut/agent-tool-drone-cli) | `pipx install agent-tool-drone-cli` | Drone CI — builds, failing-step logs, promotions |
| [**grafana-cli**](https://github.com/alexander-zierhut/agent-tool-grafana-cli) | `pipx install agent-tool-grafana-cli` | Grafana — log discovery, health scan, alert routing |
| [**openproject**](https://github.com/alexander-zierhut/agent-tool-openproject-cli) | `pipx install agent-tool-openproject-cli` | OpenProject — work packages, time, invoicing |
| [**lexware-office**](https://github.com/alexander-zierhut/agent-tool-lexware-office-cli) | `pipx install agent-tool-lexware-office-cli` | Lexware Office — invoices, contacts, AR-aging |

They compose over the shared contract:
`drone-cli wait --commit HEAD && grafana-cli scan --since 10m` answers *"my commit
built — is it healthy in production?"*

## The name — and one thing to know about it

The command is **`grafana-cli`**, matching the distribution
(`agent-tool-grafana-cli`) and the sibling tools (`drone-cli`, `openproject`).

**There is a name collision, and it is worth knowing before you install.**
Grafana ships its own `grafana-cli` binary with every *server* install — it does
plugin management and admin tasks (`grafana-cli plugins install …`). Grafana also
has a newer `grafanactl` for dashboards-as-code. This tool is neither.

In practice they rarely meet: Grafana's `grafana-cli` lives on the Grafana
*server*, and this one lives on your *workstation* or in CI, where no Grafana
server is installed. If you do end up with both on one machine, whichever comes
first on `PATH` wins — so either don't do that, or invoke this one explicitly:

```bash
python -m grafanacli guide      # always this tool, whatever PATH says
```

If you would rather not take that chance, install it under a different name:

```bash
pipx install agent-tool-grafana-cli
ln -s "$(pipx environment --value PIPX_BIN_DIR)/grafana-cli" ~/.local/bin/graf
```

## Docs

- [`docs/COMMANDS.md`](docs/COMMANDS.md) — the full command reference, generated
  from the tree (CI fails if it drifts).
- [`AGENTS.md`](AGENTS.md) — the working agreement for anyone, human or model,
  changing this repo.
- [`spike/VERIFIED_FINDINGS.md`](spike/VERIFIED_FINDINGS.md) — what the live API
  actually does, as measured. Trust it over the docs — and over itself: one of its
  findings was wrong and a live test caught it.

## License

MIT — see [LICENSE](LICENSE).
