# agent-tool-grafana-cli

Agent-ready CLI for **Grafana**, from a developer's perspective: *find out what
you can even get logs from, then get them* — and set up an alert so you hear
about it next time.

```bash
pipx install agent-tool-grafana-cli
graf guide
```

## Contributing takes two commands

```bash
pip install -e '.[test]' && pytest
```

Green on a clean checkout. **No Docker, no Grafana, no token, no network** — the
hermetic suite runs in about two seconds. Live tests skip themselves unless you
point them at a real server; a missing token is the normal case, not a failure.

## What it's for

You deployed. Something feels wrong. Fifteen seconds:

```bash
graf logs sources              # what can I even get logs from?
graf scan --since 15m          # is it broken? what should I look at first?
graf logs similar "<a line>"   # has this happened before, or elsewhere?
graf alert create --title "..." -q '<query>' --folder <uid>
                               # ...and it tells you whether that alert would
                               # actually reach a human
```

It pairs with the siblings: `drone-cli wait --commit HEAD && graf scan --since 10m`
answers *"my commit built — is it healthy in production?"*

## The killer feature: discovery

Grafana will not tell you, anywhere in the UI or the API, **what you can get logs
from**. You are expected to already know. Answering it means enumerating
datasources, filtering to the log-capable types, hitting each one's label API, and
then counting the values behind every label.

That last step is the point:

```
$ graf logs sources
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
graf alert route <uid>
```

walks rule → labels → policy tree → receiver → integrations and says, in words,
that nobody will be notified. `graf notify check` does it for every rule at once.

## Multiple organisations

A Grafana service-account token is **hard-scoped to one org** — there is no header
or flag that widens it. So multi-org is one profile per org:

```bash
graf auth login                     # org A -> profile "default"
graf auth login --profile sales     # org B -> profile "sales"
graf -p sales logs sources
```

Datasource uids differ per org, so sticky defaults (`graf context`) are stored per
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
- `graf guide` is the built-in manual; it works with no config, no token and no
  network, because that is exactly when you need it.

## Requirements

A **service account token with the Admin role** (`<your-grafana>/org/serviceaccounts`).
Admin is not gratuitous: listing datasources needs `datasources:read`, which is org
Admin by default. An Editor token can query a datasource whose uid it already knows
but cannot discover any — which is the whole point of the tool. `graf server doctor`
reports exactly what your token can do.

Works against Grafana with **Loki** (logs) and **Prometheus/Mimir/Thanos/Cortex**
(metrics). Verified against Grafana 13.0.3.

## Part of the family

Built on [agent-tool-shared-cli](https://github.com/alexander-zierhut/agent-tool-shared-cli):
JSON on stdout, JSON errors on stderr, stable exit codes, `--dry-run`, and a
built-in `guide` so an agent can learn the tool from the tool.

Siblings: [openproject](https://github.com/alexander-zierhut/agent-tool-openproject-cli) ·
[drone](https://github.com/alexander-zierhut/agent-tool-drone-cli)

## The name

The command is **`graf`**, not `grafana-cli`.

`grafana-cli` is the **official binary that ships with every Grafana server
install** (plugin management), and `grafanactl` is Grafana's own newer
dashboards-as-code tool. Shadowing either is the mistake we deliberately refused
with `op` (OpenProject) and `drone` — a package manager should never win a PATH
fight it didn't announce. The distribution keeps the family name
(`agent-tool-grafana-cli`); only the command is short.

## Docs

- [`docs/COMMANDS.md`](docs/COMMANDS.md) — the full command reference, generated
  from the tree (CI fails if it drifts).
- [`AGENTS.md`](AGENTS.md) — the working agreement for anyone, human or model,
  changing this repo.
- [`spike/VERIFIED_FINDINGS.md`](spike/VERIFIED_FINDINGS.md) — what the live API
  actually does, as measured. Trust it over the docs — and over itself: one of its
  findings was wrong and a live test caught it.

## License

MIT
