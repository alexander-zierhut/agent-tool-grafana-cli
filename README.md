# agent-tool-grafana-cli

Agent-ready CLI for **Grafana**, from a developer's perspective: *find out what you
can even get logs from, then get them.*

> 🚧 **Scaffold.** Published early so the release pipeline is proven; the command
> surface lands next.

```bash
pipx install agent-tool-grafana-cli
```

Part of the `agent-tool-<x>-cli` family, built on
[agent-tool-shared-cli](https://github.com/alexander-zierhut/agent-tool-shared-cli):
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

## License

MIT
