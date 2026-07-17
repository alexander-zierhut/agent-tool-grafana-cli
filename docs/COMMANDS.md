# Command reference

_Auto-generated from the CLI (`python scripts/gen_docs.py`)._

_Every command also accepts `--output/-o` (json\|table\|markdown\|csv), `--format/-f`, `--fields`/`--columns`, `--dry-run`, `--stream` and `--no-context`. Those are **stripped from argv before parsing**, so they work anywhere on the line — before or after the subcommand. `--profile/-p` and `--no-color` are ordinary root options and must therefore come **before** the subcommand (`graf -p sales logs sources`, not `graf logs sources -p sales`)._

## Groups

- [`alert`](#alert) — Alert rules — including `alert route`: will it reach you?
- [`auth`](#auth) — Log in, log out, inspect credentials.
- [`context`](#context) — Sticky session defaults (datasource, etc.), per profile.
- [`dashboard`](#dashboard) — Dashboards: find, read, create.
- [`datasource`](#datasource) — Datasources: list, inspect, health-check.
- [`guide`](#guide) — Built-in operating guide — how to use this CLI without external docs.
- [`install`](#install) — Integrate with other tools (e.g. `install claude`).
- [`logs`](#logs) — Logs: discover sources, query, and find similar problems.
- [`metrics`](#metrics) — Metrics: PromQL against Prometheus/Mimir.
- [`notify`](#notify) — Contact points, notification policies, silences.
- [`org`](#org) — Organisations — and why a token only ever sees one.
- [`raw`](#raw) — Escape hatch: call any API endpoint directly.
- [`scan`](#scan) — Is this healthy? Find errors, panics and deprecations in one pass.
- [`server`](#server) — Health, version — and `server doctor`.
- [`settings`](#settings) — View & change CLI settings.

## `alert`

### `graf alert create`

Create an alert rule from a query — and immediately say who it can reach.

POSTs to `/api/v1/provisioning/alert-rules`. The body has two `data[]`
stages modeled on a real provisioned rule inspected live (see
`_query_stage`/`_threshold_stage`): your query as an instant value (`A`),
then a `threshold` expression (`B`) that reduces and compares it. The
rule's top-level `condition` field is set to `"B"` — Grafana's name for
"which stage is the pass/fail one", a different thing from the evaluator
`--condition` above despite the shared word.

`notification_settings` is deliberately OMITTED from the body — leaving it
unset routes the new rule through the notification policy tree by label,
which is the whole point of a tool built around `alert route`. Pinning it
to one receiver here would silently bypass that.

**Every read below (org id, folder discovery, datasource resolution) still
executes under `--dry-run`** — only the final `client.post` is intercepted,
by the transport, so the printed body is the real one, not a guess.

After a real (non-dry-run) create, this runs `route`'s own join
(`routing.delivery_report`) against the new rule's labels and folds the
result into the output under `delivery`, with a `warning` field if nothing
would actually be delivered. Creating an alert that cannot notify anyone
is the exact failure this tool exists to prevent — this must say so at
creation time, not months later when a whole fleet of rules are firing
into a receiver with zero integrations and every Grafana screen looks fine
(see `routing`'s module docstring for the live instance that motivated this).

UNVERIFIED against a real create (this was built without writing to the
live instance): the exact acceptance of an omitted `notification_settings`
and of evaluator types other than `"gte"`. Both degrade honestly — a
rejected shape comes back as a `ValidationError` naming what the server
did not like, not a silent wrong rule.

| Option | Description |
| --- | --- |
| `--title` | Rule title. **(required)** |
| `--datasource`, `-d` | Datasource uid or name. Required if the org has more than one Loki/Prometheus datasource. |
| `--query`, `-q` | LogQL or PromQL. Must reduce to a single scalar per evaluation (e.g. count_over_time(...) for Loki), not raw log lines. **(required)** |
| `--condition` | Threshold evaluator: gte (verified live), gt/lt/lte (documented, not independently verified here). |
| `--threshold` | Threshold value. Default 0.0 + gte means: fire the moment the query returns anything at all — 'let me know if this happens again'. |
| `--folder` | Folder UID. Required unless exactly one real folder is writable (`sharedwithme` never counts). |
| `--group` | Rule group name. |
| `--for` | Pending period before a firing condition actually fires, e.g. 5m, 1h. |
| `--label` | k=v (repeatable). THESE are what the notification policy tree matches on — check with `route` before relying on one. |
| `--annotation` | k=v (repeatable). Free-form context on the firing alert; never used for routing. |
| `--summary` | Shorthand for --annotation summary=...; wins over an explicit --annotation summary=. |

### `graf alert delete`

Delete an alert rule. Irreversible — its evaluation state is gone.

(Notifications Grafana already SENT for it are not recalled; only future
evaluation stops.)

**Arguments:** `uid` (required)

| Option | Description |
| --- | --- |
| `--yes`, `-y` | Skip confirmation. Required when not on a TTY. |

### `graf alert firing`

Currently firing instances — `GET /api/alertmanager/grafana/api/v2/alerts`.

Shows which receiver(s) each instance is *currently* routed to (the API
returns this directly, no join needed) — but NOT whether that receiver can
actually deliver. A receiver name here can be the empty-integrations trap
this whole module exists to catch; cross-check with
`graf notify check` or `graf alert route <rule-uid>` before trusting that
"routed to X" means "reached someone".

The three filters are the standard Alertmanager v2 query params
(`active`/`silenced`/`inhibited`, each tri-state: omit to include both).
They are documented upstream Alertmanager API, not independently
re-verified against this instance with every combination — flag if one
behaves unexpectedly.

| Option | Description |
| --- | --- |
| `--active`, `--no-active` | Only (or exclude) alerts in the 'active' state. |
| `--silenced`, `--no-silenced` | Only (or exclude) silenced alerts. |
| `--inhibited`, `--no-inhibited` | Only (or exclude) inhibited alerts. |

### `graf alert get`

Show one alert rule — `GET /api/v1/provisioning/alert-rules/{uid}`.

This is the raw provisioning shape: `data[]` carries the query stages
verbatim, including the `__expr__` reduce/threshold stage that most UIs
hide. Use `route {uid}` for "who does this actually reach" — this command
only shows what the rule IS, not what it DOES.

**Arguments:** `uid` (required)

### `graf alert list`

List alert rules — `GET /api/v1/provisioning/alert-rules`.

This endpoint returns every rule in the org in one response, with no
pagination and no server-side filter params (verified: the client's own
docs note the provisioning API "returns everything at once"). So `--folder`
and `--limit` are both applied client-side, after the fact — cheap here
because the live instance has a handful of rules, not thousands.

| Option | Description |
| --- | --- |
| `--folder` | Only rules in this folder UID (exact match — Grafana uids are case-sensitive). |
| `--limit` | Max rules to return. Default: the configured default_limit (100). |

### `graf alert pause`

Pause an alert rule — stop evaluating it without deleting it.

UNVERIFIED SHAPE: this was built without a live write to avoid mutating a
real, currently-alerting instance. `PUT /api/v1/provisioning/alert-rules/{uid}`
replaces the whole rule (that much is the documented provisioning-API
contract); this fetches the rule GET already proved valid, flips only
`isPaused`, and PUTs the same object back — the smallest possible change to
a body already known to be acceptable, rather than constructing a partial
body from scratch. If this 400s, the shape assumption is wrong: report it.

**Arguments:** `uid` (required)

### `graf alert route`

If this rule fires, who gets told? THE reason this module exists.

Grafana gives you the rule, the policy tree and the receivers as three
separate objects and joins none of them (see `routing.delivery_report`).
An alert can fire forever into a receiver with zero integrations and every
screen in the Grafana UI will still show it "firing" — never "undelivered",
because Grafana has no such state. This command computes it.

Pass a rule UID for the real question, or `--label` alone for a
hypothetical one — worth answering *before* you write the rule, not after
it has been silently firing into the void for a month:

    graf alert route efhvhftr6yxhce
    graf alert route --label severity=critical --label team=payments
    graf alert route efhvhftr6yxhce --label severity=critical   # tweak one rule's labels

Never raises for "undelivered" — that is the finding, not an error. Gate a
non-zero exit behind `--exit-code` if a script needs to branch on it.

**Arguments:** `uid` (optional)

| Option | Description |
| --- | --- |
| `--label` | k=v (repeatable). With no UID: route this hypothetical label set — 'what if an alert with severity=critical fired?' With a UID: merged onto that rule's own labels — 'what if THIS rule also carried severity=critical?' |
| `--exit-code` | Exit 20 if nothing would be delivered. Default: always exit 0 — an undeliverable route is an observed fact about Grafana's config, not a failure of this CLI. |

### `graf alert unpause`

Resume a paused alert rule. Same unverified-shape caveat as `pause`.

**Arguments:** `uid` (required)

## `auth`

### `graf auth login`

Log in and store the token in your OS keyring.

Interactive when flags are omitted: asks for the server, then — knowing the
URL — shows exactly where to get a token, rather than making you hunt for it.

The org is never asked for; see the module docstring for why. It is read
back from the token itself (`GET /api/org`) and stored on the profile.

| Option | Description |
| --- | --- |
| `--url`, `-u` | Grafana server URL, e.g. https://grafana.example.com. |
| `--token`, `-t` | Service-account token. Get one at <url>/org/serviceaccounts. |
| `--profile` | Profile to write. One profile per Grafana ORG — a token cannot cross orgs, so a second org means logging in again with --profile <name> and a token minted THERE. Defaults to the currently active profile. |
| `--insecure` | Skip TLS verification (self-signed certs). |

### `graf auth logout`

Remove the stored token for a profile.

The profile's server/org config is left in place — `graf auth login
--profile <name>` re-populates just the token, without re-typing the URL.

| Option | Description |
| --- | --- |
| `--profile` | Profile to log out (default: the active one). |

### `graf auth profiles`

List every configured profile — the multi-org overview.

One profile per Grafana org is the whole multi-org model here (see
`config.py`'s module docstring), so this is the command that answers "which
orgs do I have set up, and which one is active right now?".

### `graf auth status`

Show the active profile, its org, and — crucially — WHICH token/backend
is actually in use.

Precedence is env > keyring > file (see `credentials.py`), deliberately: it
is what lets this tool run non-interactively in CI without touching a
keyring that isn't there. But that also means an exported `GRAFANA_TOKEN`
silently overrides a keyring `graf auth login`, confusing exactly when you
can least afford it — so this command always names the backend that will
actually speak, never just whether *a* token exists.

## `context`

### `graf context clear`

Clear the active profile's context entirely. Other profiles' contexts are untouched.

### `graf context set`

Set/merge sticky defaults for the ACTIVE PROFILE.

    graf context set --datasource P1A2B3C4

Then `graf logs query` behaves like `graf logs query --datasource P1A2B3C4`,
but only while this profile stays active — switch `--profile` and this
context does not follow, because the UID would not mean anything there.

| Option | Description |
| --- | --- |
| `--datasource` | Default datasource uid (or name) for logs/metrics commands. Per-org — see below. |
| `--since` | Default lookback window for this profile, e.g. 1h, 15m, 2d. Validated like `settings set-since`. |
| `--folder` | Default folder uid for dashboard/alert commands. |

### `graf context show`

Show the active PROFILE's context — each value, and where it came from.

Run this FIRST whenever output looks wrongly scoped: this is implicit state
that changes results, and nothing echoes it back on a normal command. Always
names the profile it belongs to — a context that looks empty may simply
belong to a *different* profile than the one you think is active.

Every key is reported as `{"value": ..., "from": ...}` — `from` is `saved`
for everything, for now. Read `applies` before believing any of it:
`--no-context` suspends the whole context for one command, and then these
values are saved but NOT in force.

## `dashboard`

### `graf dashboard create`

Create a dashboard — `POST /api/dashboards/db`.

Two ways in:

* `--file PATH` — upload a dashboard JSON you already have (hand-written,
  exported, or round-tripped from `dashboard get --out`).
* `--title` + `--query`/`-q` (+ `--datasource`, `--panel-type`) — build the
  minimal valid dashboard from scratch: one panel, one target per `-q`.
  This is the point of the command — "make me a dashboard for the thing I
  just found in the logs" should not require hand-writing panel JSON.

**Optimistic locking.** Grafana saves are versioned via `dashboard.version`;
saving over a newer version answers **412**, which this CLI's client maps
to `ConflictError` (exit 6). `--overwrite` is what you reach for when that
happens — but it only has something TO overwrite when the dashboard JSON
already carries the real `uid` it is updating, which in practice means the
`--file` round trip (`get --out` -> edit -> `create --file --overwrite`).
The `--title`/`--query` path builds a dashboard with no `uid` at all, so
each run creates a genuinely new dashboard — there is nothing yet to
conflict with, and `--overwrite` is a no-op there until you also pass a
`--file` built from a previous `get`.

Respects `--dry-run` automatically (the client intercepts every POST
before it reaches the network) — the request that would be sent, dashboard
JSON included, is printed instead.

| Option | Description |
| --- | --- |
| `--file` | A dashboard JSON file to upload as-is. Accepts either a bare dashboard object or the full {dashboard, meta} envelope `dashboard get` writes — the envelope is unwrapped automatically, so `get --out d.json` then `create --file d.json --overwrite` round-trips. |
| `--title` | Dashboard title. Required unless --file supplies one (an explicit --title with --file renames it). |
| `--query`, `-q` | A LogQL/PromQL query to plot (repeatable — each becomes one target/series in a single panel). Required, at least once, when not using --file. |
| `--datasource`, `-d` | uid or name of the datasource the queries run against. Query mode only; resolved the same way as `logs query`/`metrics query` — explicit > sticky context > the only candidate of the right kind. |
| `--panel-type` | timeseries \| logs \| stat. Query mode only. |
| `--folder` | Folder UID to save into (see `dashboard folders`). Omit for General. |
| `--overwrite` | Allow overwriting an existing dashboard. See docstring: without a stable uid (--file mode only) there is nothing to overwrite, so this mostly matters there. |
| `--message` | Commit message, shown in the dashboard's version history. |

### `graf dashboard delete`

Delete a dashboard — `DELETE /api/dashboards/uid/{uid}`. Irreversible.

No soft-delete, no trash: the dashboard's JSON, its panels and its version
history are gone. Any alert rule or link pointing at this uid starts
404ing on the next click.

**Arguments:** `uid` (required)

| Option | Description |
| --- | --- |
| `--yes`, `-y` | Skip confirmation. |

### `graf dashboard folders`

List folders — `GET /api/folders`.

Marks the virtual "Shared with me" folder (`uid: sharedwithme`, live:
`id: -1`) as `virtual: true`. It is not a save target: it is Grafana's own
UI grouping, not a real folder, and `create --folder sharedwithme` is a
trap this command exists to let a caller avoid before hitting it.

### `graf dashboard get`

Fetch one dashboard — `GET /api/dashboards/uid/{uid}` -> `{dashboard, meta}`.

The envelope is returned whole, including `meta` (created/updated,
`folderUid`, `version`, who last saved it) — dropping it would throw away
exactly the fields `create --overwrite` needs to update this dashboard in
place later. `--out` writes that same envelope to a file, so
`dashboard get UID --out d.json` followed by `dashboard create --file
d.json --overwrite` is a working round trip with no hand-editing of shape.

**Arguments:** `uid` (required)

| Option | Description |
| --- | --- |
| `--out` | Write the JSON to this file instead of stdout. NOT --output: that flag name is a reserved global (the output *format*) and is stripped from the command line before this command ever sees it — a sibling tool shipped exactly that collision for four releases (the path was silently swallowed as a format, and the write landed in the wrong place with exit 0). |

### `graf dashboard list`

List dashboards — `GET /api/search?type=dash-db`.

This is the browsing command; if you already have a name in mind, `search`
reads better. `--folder` takes a UID (`dashboard folders` prints them),
never a title — Grafana's search API does not accept folder names.

NOTE: the folder-filter query parameter (`folderUIDs`) is written against
Grafana's documented modern search API but was not independently exercised
live against this instance — if it 400s on your server, `--folder` may
need the older `folderIds` on an install this old.

| Option | Description |
| --- | --- |
| `--folder` | Folder UID to filter to (see `dashboard folders`). |
| `--tag` | Filter by tag (repeatable; Grafana ANDs multiple tags). |
| `--limit` | Max dashboards (default: the configured default_limit). |

### `graf dashboard panels`

What does this dashboard actually query? THE reason this group exists.

Walks every panel — descending into collapsed rows, see the module
docstring — and reads each query target's `expr` (LogQL/PromQL),
`rawSql`, or `target` (legacy Graphite-style), whichever is present,
alongside the datasource it runs against. Where the datasource type makes
the query language unambiguous (`loki` -> LogQL, `prometheus` -> PromQL —
Mimir/Thanos/Cortex all report as `prometheus`), a ready-to-run
`graf logs query` / `graf metrics query` command is included so you can
run the same query yourself with a different window, without first
reverse-engineering which field in the target JSON is the query.

A query with no obvious language (a `rawSql` panel, or a plugin datasource
this CLI does not classify) still gets reported — just without a
`suggested` command, because guessing wrong there would be worse than
saying nothing.

**Arguments:** `uid` (required)

### `graf dashboard search`

Find dashboards by name — `GET /api/search?query=TERM&type=dash-db`.

"Find me the dashboard about X" is the question this answers; it is the
same endpoint as `list` with a `query` term added, split into its own
command because that is how the question actually gets asked.

**Arguments:** `term` (required)

| Option | Description |
| --- | --- |
| `--limit` | Max results (default: the configured default_limit). |

## `datasource`

### `graf datasource get`

Show one datasource's config — resolved by uid or name.

Grafana exposes uid and name lookup as two different endpoints and gives
no hint which kind of string you are holding, so this tries uid first
(`GET /api/datasources/uid/{ref}`) and falls back to name
(`GET /api/datasources/name/{ref}`) on a 404 — a human reads names off the
UI, an agent copies uids out of JSON, and refusing either is a papercut.

**Never prints a secret VALUE.** Grafana's API already enforces this —
`secureJsonData` (the actual secrets: passwords, tokens, custom headers)
is write-only and never appears in a GET response; `secureJsonFields` only
ever carries booleans saying WHICH fields are set. That said, this command
surfaces those booleans deliberately rather than treating the whole
section as noise: on this instance one Loki datasource injects a secret
header (`X-Loki-Label-Preset`) that scopes every query to a tenant, and
"this datasource has a secret header configured" is exactly the fact
someone debugging a tenant-scoping problem needs — even though they can
never see the header's value through this API.

**Arguments:** `ref` (required)

### `graf datasource health`

Ask Grafana to test one datasource's connectivity.

Uses `sources.health`, which reports rather than raises: not every
datasource plugin implements the health-check resource, so a failure here
can mean "the backend is unreachable" or "this plugin has no health check
at all" — both are reported as `ok: false` with the server's own message
rather than one of them crashing the command.

**Arguments:** `ref` (required)

### `graf datasource list`

Every datasource in this org, classified by what this CLI can do with it.

Needs `datasources:read`, which Viewer already has — see the module docstring. `logs`/
`metrics` are `"supported"` (this CLI can query it), `"recognised"` (it is
a known log/metric-capable type, e.g. Elasticsearch, but not implemented
here yet), or `null` (neither — a plugin datasource, or one that is
neither a log nor a metric source, e.g. a SQL datasource used only for
dashboards).

### `graf datasource test`

Health-check EVERY datasource in this org, in one pass.

Each datasource's failure is captured into its own row, never raised —
the same contract as `logs sources`/`scan`: one dead backend must not
blank out the report for every healthy one sitting next to it. Verified
live: this instance has exactly one datasource whose backend is down
(a 502 with an empty body through the proxy, which the client maps to
`DatasourceUnreachable`); it shows up here as one `ok: false` row with the
reason, beside every other datasource reporting `ok: true`. Nothing about
a failed health check raises this command's own exit code — read `results`
(and `healthyCount` vs `datasourceCount`) rather than the process exit.

## `guide`

### `graf guide`

Built-in operating guide — how to use this CLI without external docs.

**Arguments:** `topic` (optional)

## `install`

### `graf install claude`

Register this CLI with Claude Code as a Skill so Claude auto-uses it.

Writes ~/.claude/skills/grafana/SKILL.md (idiomatic discovery). Claude then
invokes `graf` whenever you mention Grafana, Loki logs or dashboards.
Reversible with --uninstall.

| Option | Description |
| --- | --- |
| `--project` | Install into ./.claude (this repo) instead of ~/.claude. |
| `--memory` | Also add a one-line hint to ~/.claude/CLAUDE.md. |
| `--force` | Install even if Claude Code isn't detected. |
| `--uninstall` | Remove the skill (and memory hint). |
| `--print` | Print the SKILL.md that would be written and exit. |

## `logs`

### `graf logs levels`

The `detected_level` distribution per datasource — where should I look?

One query per datasource against a match-all selector, then a count of
`detected_level` values and the top problem clusters (via `analysis.cluster`,
same engine as `graf scan`) among them. This is deliberately the cheapest
possible answer to "where is it worse right now" — one query per source,
not a query per host or per error category.

The counts are exact for what was FETCHED, not for the whole window: each
datasource is sampled up to ``--limit`` lines (newest first), so on a busy
source with more log volume than ``--limit`` this under-counts older
levels in the window rather than over-claiming a total nothing here
actually counted. Narrow the window or raise ``--limit`` for a fuller
picture; this command trades completeness for being cheap enough to run
before every other one.

| Option | Description |
| --- | --- |
| `--datasource`, `-d` | Restrict to one datasource. Default: every log datasource. |
| `--since` | How far back, e.g. 1h, 2d, 30m. Default: your configured default-since. |
| `--from` | Explicit window start. Overrides --since. |
| `--to` | Explicit window end. Default: now. |
| `--limit` | Max lines sampled per datasource. Default: your configured default-limit. |

### `graf logs query`

Read logs from one datasource.

The LogQL actually sent is ALWAYS echoed back in the payload's ``query``
field, raw or not — a query an agent cannot see is a query it cannot fix,
and that includes queries it built itself from ``--label``/``--contains``,
since the escaping and operator handling happen here, invisibly, unless
you look.

GOTCHA worth knowing before reaching for ``--query``: ``detected_level`` is
not a real stream label — it does not appear in ``/labels`` — yet Loki
derives it at query time and lets you filter on it. ``{detected_level=
"error"}`` in a raw selector silently matches NOTHING; ``--level error``
(or, in raw LogQL, ``| detected_level="error"`` as a pipeline stage after
the selector) is the only form that works. `loki.build_query` gets this
right for you when you use ``--level``; a hand-written ``--query`` will not
warn you if you get it wrong.

With no ``--label`` at all (and no ``--query``), this picks the
highest-cardinality label discovered in the window and queries
``{that_label=~".+"}`` — Loki rejects an empty ``{}`` selector outright, so
"give me everything" has to be spelled as *some* real matcher. Run
`graf logs sources` first if that surprises you.

| Option | Description |
| --- | --- |
| `--datasource`, `-d` | Datasource uid or name. Default: sticky context, or the only log datasource. |
| `--label`, `-l` | k=v stream matcher, repeatable. Value may carry an operator prefix: ~ regex, ! negate, e.g. hostname=~web.* |
| `--contains` | Line must contain this substring (LogQL \|=), repeatable — ANDed together. |
| `--exclude` | Line must NOT contain this substring (LogQL !=), repeatable. |
| `--regex` | Line must match this regex (LogQL \|~, RE2 syntax). |
| `--level` | Filter by detected_level (trace, debug, info, warn, error, fatal, critical, unknown, not enforced — Loki may add more). Applied as a pipeline stage, never a selector — see the gotcha below. |
| `--query`, `-q` | Raw LogQL, used VERBATIM. Mutually exclusive with --label/--contains/--exclude/--regex/--level. |
| `--limit` | Max lines returned. Default: your configured default-limit. |
| `--since` | How far back, e.g. 1h, 2d, 30m. Default: your configured default-since. |
| `--from` | Explicit window start (RFC 3339, unix timestamp, or 'now'). Overrides --since. |
| `--to` | Explicit window end. Default: now. |
| `--direction` | backward (newest-first, default) or forward (oldest-first) — which end of the window --limit keeps when there are more matches than that. |
| `--raw` | Print bare log lines as text instead of JSON — the one carve-out from this tool's output contract, because logs are prose. |

### `graf logs search`

"I don't know what my thing is called" — find out.

You know roughly what you are looking for ("the api service", "that
postgres box") but not the exact label value LogQL needs. This checks
every label VALUE on every log datasource for the term as a substring —
``search api`` finds ``systemd_unit=api.service`` even though you never
typed the ``.service`` suffix — and, with ``--content``, also runs a real
query to find which label SETS carry the term in the log lines
themselves.

The point is not the list of hits, it is the ``suggestion`` field on each
one: a ready-to-run ``graf logs query ...`` command with the right
``--datasource``/``--label`` (and ``--contains``, for a content hit) already
filled in. A label name is not a query; this turns the guess into one.

Ignores any sticky ``--datasource`` context by default, on purpose — the
whole reason to run `search` instead of `query` is that you have not
committed to a datasource yet.

**Arguments:** `term` (required)

| Option | Description |
| --- | --- |
| `--datasource`, `-d` | Restrict to one datasource. Default: every log datasource — this command's job is telling you WHICH one. |
| `--content` | Also search log CONTENT (a \|~ regex query against a match-all selector), not just label values. Costs one real query per datasource — slower than the default. |
| `--since` | How far back, e.g. 1h, 2d, 30m. Default: your configured default-since. |
| `--from` | Explicit window start. Overrides --since. |
| `--to` | Explicit window end. Default: now. |
| `--limit` | Cap on --content query results per datasource. Default: your configured default-limit. |

### `graf logs similar`

"Has this happened elsewhere?" — search by SHAPE, not exact text.

A raw substring search would only find the exact ids in the line you
already have. This reduces the line to its shape with
`analysis.fingerprint` — ids, timestamps, addresses and durations become
placeholders, so ``connection to 10.0.0.7:5432 failed after 1.2s`` and
``connection to 10.0.0.9:5432 failed after 0.4s`` are recognised as the
SAME problem — then turns that shape into a permissive LogQL regex with
`analysis.to_regex` and runs it, unanchored, against a match-all selector
on every log datasource (or one, with ``--datasource``).

Results are grouped by `analysis.cluster`, which reports which distinct
label sets (host, unit, container — whichever is most specific) the
pattern showed up on and how many times: "this happened on one host" and
"this happened on all twenty-one" look identical in a flat log tail and
are completely different problems.

The fingerprint and the regex derived from it are always in the payload —
the regex especially, since a normalised fingerprint alone does not tell
you whether `to_regex` generalised more or less than you would have by
hand.

**Arguments:** `line` (optional)

| Option | Description |
| --- | --- |
| `--line` | Same as the positional LINE — for scripting where a positional string is awkward. |
| `--from-last` | Use the most recent error-level line instead of typing one. |
| `--datasource`, `-d` | Restrict to one datasource. Default: every log datasource — 'elsewhere' means the whole org, not just where you found it. |
| `--since` | How far back, e.g. 1h, 2d, 30m. Default: your configured default-since. |
| `--from` | Explicit window start. Overrides --since. |
| `--to` | Explicit window end. Default: now. |
| `--limit` | Max lines fetched per datasource. Default: your configured default-limit. |

### `graf logs sources`

"What can I even get logs from?" — run this before anything else.

Grafana will not answer this question anywhere, UI or API: you are expected
to already know which datasource, and which label, to query. This
enumerates every log-capable datasource in the org and, for each Loki one,
every label it has IN THIS WINDOW along with how many distinct values it
carries — cardinality, not just names. On the reference instance two of
Loki's four labels have exactly one value: "you can filter on `job`" is
worthless advice when `job` never varies, and cardinality is the only way
to tell the useless labels from the ones worth building a selector on.

The window matters more than it looks: `/labels` and `/label/*/values` are
time-bounded, so this command's answer can differ at 09:00 and at 17:00.
That is why the resolved window is always in the payload, and why
`--since`/`--from`/`--to` exist here at all rather than this being a
parameterless "list everything" command.

A datasource whose backend is down is reported WITH its error, beside the
ones that are fine — one dead proxy must not blank out a working report.
A datasource of a recognised-but-unimplemented type (Elasticsearch,
CloudWatch, ...) is listed too, honestly labelled, rather than hidden:
pretending it does not exist would be the same non-answer Grafana already
gives.

| Option | Description |
| --- | --- |
| `--since` | How far back to look, e.g. 1h, 2d, 30m. Default: your configured default-since. |
| `--from` | Explicit window start (RFC 3339, unix timestamp, or 'now'). Overrides --since. |
| `--to` | Explicit window end. Default: now. |
| `--sample` | Example values shown per label. |
| `--datasource`, `-d` | Restrict to one datasource (uid or name). Default: every log datasource in the org. |

### `graf logs tail`

Follow new log lines — by POLLING, not a live stream.

Loki's own tail endpoint is a WebSocket, and this CLI's whole HTTP stack is
a synchronous `httpx.Client`; adding a websocket dependency for one command
is not a trade worth making. So this calls the exact same `query_range` the
`query` command uses, on a timer, tracking the newest timestamp it has
already shown you and asking only for what came after.

Be honest with yourself about what that means:

* a line can be up to ``--interval`` seconds late — it only appears at the
  NEXT poll, never sooner;
* if MORE than ``--limit`` new lines land within one interval, only the
  newest ``--limit`` of them are kept (this polls with ``direction=
  backward``, same as `query`'s default) — the rest, being older than
  the ones already reported and now outside the next poll's start bound,
  are gone for good, not delayed. A bursty source needs a higher
  ``--limit`` or a shorter ``--interval``, not patience.

Ctrl-C stops it. This deliberately does not catch `KeyboardInterrupt` —
letting it propagate is what gives you the standard exit code 130 instead
of this command inventing its own "stopped" status.

With `--stream` (the global flag), each new line is written as its own
NDJSON row via `obj.emitter.stream_json`, as it is found. Without it, each
poll's new lines are emitted together as one JSON batch — so in `-o table`
or plain JSON mode, expect one printed block per poll that found something,
not one line at a time.

| Option | Description |
| --- | --- |
| `--datasource`, `-d` | Datasource uid or name. Default: sticky context, or the only log datasource. |
| `--label`, `-l` | k=v stream matcher, repeatable. Same syntax as `logs query`. |
| `--contains` | Line must contain this substring, repeatable. |
| `--exclude` | Line must NOT contain this substring, repeatable. |
| `--regex` | Line must match this regex. |
| `--level` | Filter by detected_level, e.g. error. |
| `--query`, `-q` | Raw LogQL, used verbatim. Mutually exclusive with the builder flags above. |
| `--interval` | Seconds between polls. |
| `--limit` | Max lines fetched PER POLL. Default: your configured default-limit. |

## `metrics`

### `graf metrics describe`

What IS this metric, and how do you slice it?

Three calls answer that: `/api/v1/metadata` (type + help text — best-effort,
scraped from a target's `/metrics` endpoint, so a metric can be real and
still have no metadata if the target that describes it is down), the label
NAMES actually present on it (from a `/api/v1/series` sample, since
Prometheus has no "labels of this metric" endpoint), and a handful of real
series so you can see values, not just names.

**Arguments:** `metric` (required)

| Option | Description |
| --- | --- |
| `--sample` | How many example series to include, from /api/v1/series. |
| `--since` | Window to look for series in (default: default_since). |
| `--from` | Explicit window start. |
| `--to` | Explicit window end. |
| `--datasource`, `-d` | Metrics datasource uid or name. |

### `graf metrics labels`

Label NAMES this datasource carries, or (with `--label`) that label's values.

Not the same question as `metrics list`: `list` is metric NAMES
(`__name__`'s own values); this is everything else you can slice a PromQL
query by — `job`, `instance`, and whatever else targets export.

| Option | Description |
| --- | --- |
| `--label` | Show this label's VALUES instead of the label names. |
| `--since` | Only labels/values seen since this far back. |
| `--from` | Explicit window start. |
| `--to` | Explicit window end. |
| `--limit`, `-n` | Max values returned with --label (default: default_limit). |
| `--datasource`, `-d` | Metrics datasource uid or name. |

### `graf metrics list`

Metric names this datasource knows (`__name__`).

841 live on this instance — unbounded output here is a context-window
accident, hence `--limit`/`--filter`. `--since`/`--from`/`--to` are OPTIONAL
(unlike Loki's label endpoints, which the CLI always time-bounds): omitted,
Prometheus answers from its full retained data, which is what "841 live"
above was counted against.

| Option | Description |
| --- | --- |
| `--filter` | Only names containing this substring (case-insensitive). |
| `--limit`, `-n` | Max names returned (default: the configured default_limit; 0 = no cap). |
| `--describe` | Enrich each name with its type + help text (one extra call to /api/v1/metadata) — the difference between a wall of names and something you can actually pick from. |
| `--since` | Only names seen since this far back. |
| `--from` | Explicit window start. |
| `--to` | Explicit window end. |
| `--datasource`, `-d` | Metrics datasource uid or name. |

### `graf metrics query`

Run a PromQL query. Instant by default; `--range` for a time series.

The window (`--since`/`--from`/`--to`) always resolves, even for an instant
query: its END becomes the instant `time=` sent to Prometheus, so `--to
2026-07-10T00:00:00Z` answers "what was this metric at that instant"
without needing a separate flag for it.

| Option | Description |
| --- | --- |
| `--query`, `-q` | PromQL expression. **(required)** |
| `--range` | query_range (a time series) instead of an instant query. |
| `--since` | How far back the window starts, e.g. 1h, 30m. |
| `--from` | Explicit window start (RFC3339, unix ts, or 'now'). |
| `--to` | Explicit window end / instant time. Default: now. |
| `--step` | query_range resolution step: seconds, or a duration like '30s'/'5m'. Default: computed from the window so the point count stays under Prometheus's cap. |
| `--datasource`, `-d` | Metrics datasource uid or name. |

### `graf metrics up`

Run `up` and report which scrape targets are down.

This is the single most useful PromQL query there is, and the metrics half
of "does this project work?" (`graf logs sources` / `graf scan` cover the
logs half). Every target Prometheus/Mimir scrapes reports `up` == 1 or 0 —
no PromQL knowledge required to ask "is anything broken right now".

| Option | Description |
| --- | --- |
| `--datasource`, `-d` | Metrics datasource uid or name. |
| `--exit-code` | Exit 20 if any target is down. Default: exit 0 regardless — a down target is an OBSERVATION this command made successfully, not a CLI failure. |

## `notify`

### `graf notify check`

Audit EVERY alert rule's delivery in one pass — is alerting actually wired up?

Runs `routing.rule_labels` -> `routing.delivery_report` for each rule
returned by `GET /api/v1/provisioning/alert-rules`, against one shared
fetch of the policy tree and receivers (not one fetch per rule — the tree
and receiver list are the same for every rule in an org, so this is O(1)
network calls beyond O(rules)). Folder-title lookups are cached per
`folderUID` for the same reason: several rules typically share a folder.

This is the command to run after touching alerting config at all, or on a
schedule: it is the only way to learn "3 of 12 rules fire into a receiver
with no integrations" in one call instead of re-deriving it per rule.

| Option | Description |
| --- | --- |
| `--exit-code` | Exit 20 if ANY rule would not be delivered. Default: always exit 0. |

### `graf notify list`

Contact points — merged from BOTH alerting APIs, because they disagree.

See the module docstring for the live disagreement this reads around. The
merge priority is deliberate: a name's integrations come from the
alertmanager view first (that is what actually dispatches), falling back
to the provisioning view only if the alertmanager side reported none —
covering a contact point defined via provisioning that has not reached the
active config yet.

`usable: false` is the point of this command: a contact point can exist,
have a name, appear in `graf alert route`'s output as a real receiver, and
still deliver to nobody. Every screen in Grafana's own UI shows that case
as configured and healthy.

### `graf notify policies`

The notification policy tree — `GET /api/v1/provisioning/policies`.

Default output is a flat table (path -> receiver -> matchers): the raw
tree nests routes inside routes inside routes, and reading label-based
routing out of that nesting by eye is exactly the kind of assembly this
whole tool exists to do instead of you. `--tree` gives the real nested
object back for anyone who wants to feed it into something else.

| Option | Description |
| --- | --- |
| `--tree` | Raw nested policy tree, as Grafana stores it, instead of the flattened path table. |

### `graf notify silences`

List silences — `GET /api/alertmanager/grafana/api/v2/silences`.

A silence looks like resolution from every other Grafana screen: the alert
stops paging. `state` distinguishes `active` from `expired`/`pending` — an
expired silence with a stale `endsAt` is easy to mistake for a live one at
a glance, which is exactly the kind of gap `alert route`'s `problems` field
calls out for a currently-muted route.

### `graf notify test`

Send a real test notification through one contact point.

UNVERIFIED PATH: `POST /api/alertmanager/grafana/config/api/v1/receivers/test`
was not exercised against the live instance while building this (a live
test genuinely dispatches through whatever integration is configured, and
this was built read-only against a real, in-use Grafana). The live token
does carry `alert.notifications.receivers.test:create`, confirming the
CAPABILITY exists; the exact path and body shape below are this CLI's best
reconstruction of Alertmanager's test-receiver contract, not a verified
fact. If this 404s or 400s, that is the signal the shape is wrong — report
it rather than assume the contact point is broken.

Refuses before sending if the target has zero integrations: Grafana's test
API would accept the call and deliver nothing, which looks exactly like
success and defeats the entire point of testing. Use `--dry-run` to see
the exact request this would send without sending it.

**Arguments:** `target` (required)

| Option | Description |
| --- | --- |
| `--message` | Summary text for the synthetic test alert. |

## `org`

### `graf org check`

Does the active profile's recorded org match what its token actually is?

Catches "I copied the wrong token into this profile" — the config still
says org 6, but the token pasted in during a later `auth login` actually
belongs to org 1. Left unchecked, every subsequent command either fails
loudly (if the profile's org id gets asserted on the wire and disagrees —
see `client._headers`) or, if the profile never recorded an org id at all,
succeeds silently against whatever org the token happens to belong to,
handing back confidently-wrong data under no particular org's name.

Modelled on `server doctor`: **this never lets `OrgMismatch` escape as a
raw error.** It is the one command in this group whose entire job is
diagnosing that exact failure, so it catches it and turns it into a
reported `ok: false` instead — deliberately, and only for this one
exception. Anything else (a flat-out bad token, a network failure) is a
different problem with a different fix and is left to propagate as
itself, exit code and all, rather than folded into this report.

### `graf org current`

The org this token is scoped to, and who the token is.

Joins `GET /api/org` (the org) with `GET /api/user` (the identity). On a
service account, `id` is always **0** and `isGrafanaAdmin` is always
**false** — verified live, and true even for a token with the org Admin
role, because server-admin and org-admin are different axes. Neither is
reported as a capability signal here; `isServiceAccount` reflects `id ==
0` purely as a fact about the token kind, not a permission check.

If the profile has an org id on record and it disagrees with what the
token actually returns, that is flagged inline — but note this can only
happen when the profile's org id was never asserted on the wire (see
`config.Profile` / `client._headers`): if it WAS asserted, the mismatch
would already have failed loudly as `OrgMismatch` before this command's
own `/api/org` call ever returned. `org check` is the command that turns
that failure into a report instead of an error.

### `graf org list`

Every org this CLI can reach — NOT every org that exists on the server.

For a normal (non-server-admin) token those are different questions and
only the first one is answerable: `GET /api/orgs` needs server admin and
403s for everyone else (see module docstring). So the honest answer here
is **the configured profiles** — one per org, by construction (see
`config.py`) — each with the org id/name recorded when you logged in, and
which one is currently active.

`GET /api/orgs` is still attempted, once, as a bonus: if this token
happens to be server admin it succeeds, and the result is folded in under
`allOrgs`, clearly labelled with the endpoint that produced it. Any
failure there (403 for the common case, or even an `OrgMismatch` if the
active profile's own record is stale) is swallowed silently — it is a
bonus rung, and its absence is not news; the profile list above is the
real answer regardless.

## `raw`

### `graf raw delete`

DELETE an endpoint. Usually returns an empty body (-> `null`), not an object.

**Arguments:** `path` (required)

| Option | Description |
| --- | --- |
| `--param`, `-P` | Query param key=value (repeatable). |

### `graf raw get`

GET an endpoint and print whatever it returns, unmodified.

``/api/health`` is the one Grafana endpoint that answers with no token at
all — useful for a bare reachability check before touching auth.

Reads always execute — the global `--dry-run` only suppresses writes.

**Arguments:** `path` (required)

| Option | Description |
| --- | --- |
| `--param`, `-P` | Query param key=value (repeatable). |

### `graf raw patch`

PATCH an endpoint with a partial JSON body. Preview any write with a global `--dry-run`.

**Arguments:** `path` (required)

| Option | Description |
| --- | --- |
| `--data`, `-d` | JSON request body. |
| `--data-file` | File containing the JSON body. |
| `--param`, `-P` | Query param key=value (repeatable). |

### `graf raw post`

POST to an endpoint with a JSON body.

E.g. `POST /dashboards/db` with `--data '{"dashboard": {...}, "folderUid": "..."}'`
creates or overwrites a dashboard. Preview any write with a global `--dry-run`.

**Arguments:** `path` (required)

| Option | Description |
| --- | --- |
| `--data`, `-d` | JSON request body. |
| `--data-file` | File containing the JSON body. |
| `--param`, `-P` | Query param key=value (repeatable). |

### `graf raw put`

PUT a full JSON body to an endpoint. Preview any write with a global `--dry-run`.

**Arguments:** `path` (required)

| Option | Description |
| --- | --- |
| `--data`, `-d` | JSON request body. |
| `--data-file` | File containing the JSON body. |
| `--param`, `-P` | Query param key=value (repeatable). |

## `scan`

### `graf scan`

Is this healthy? Find errors, panics and deprecations in one pass.

| Option | Description |
| --- | --- |
| `--datasource`, `-d` | Log datasource uid or name. Default: sticky context, or the only log datasource in this org. |
| `--label`, `-l` | Scope to the project: repeatable KEY=VALUE, e.g. --label systemd_unit=myapp.service (supports ~/!/!~ prefixes, see `logs query --help`). Omit to scan every stream on the datasource — see --limit. |
| `--since` | How far back, e.g. 30m, 2h. Default: the profile's default_since. |
| `--from` | Explicit start (RFC 3339 / unix timestamp / 'now'). Overrides --since. |
| `--to` | Explicit end. Default: now. |
| `--limit` | Max lines analysed in TOTAL across both passes, after merging (default 300). |
| `--top` | Distinct problems to report in `findings`, worst first. 0 = no cap. |
| `--category` | Only list findings in this category (one of: panic, oom, disk, fatal, cert, auth, connection, deprecation, error). Categories are HINTS that rank output, never verdicts — see the docstring. `verdict`/`summary` still cover everything found; only `findings` is filtered. |
| `--exit-code` | Exit 20 when unhealthy, instead of the default 0. See the docstring. |

## `server`

### `graf server doctor`

Diagnose the server, the token, its org, and what it can actually do —
in that order, because each rung isolates one failure before the next rung
can be confused by it. Named diagnoses:


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

| Option | Description |
| --- | --- |
| `--exit-code` | Exit 21 if the report is not fully healthy. Default: always exit 0 — diagnosing a broken server correctly is still a SUCCESSFUL run of this command. |

### `graf server health`

`GET /api/health` — unauthenticated database + version probe.

Needs no token, so it is what to run first when nothing else is working:
does the URL even point at a live Grafana? (For the full diagnosis chain,
including auth and permissions, use `server doctor`.)

### `graf server version`

The Grafana server's version — from the same unauthenticated
`/api/health` probe as `server health`, trimmed to the one field most
scripts actually want.

## `settings`

### `graf settings path`

Print the config file path.

### `graf settings set-format`

Set the default output format.

**Arguments:** `fmt` (required)

### `graf settings set-limit`

Set the default result limit.

The live instance carries ~87 units across ~21 hosts (per-label cardinality,
see `spike/VERIFIED_FINDINGS.md`), so an unbounded default is a context-window
accident waiting to happen — see `config.DEFAULT_LIMIT`.

**Arguments:** `limit` (required)

### `graf settings set-since`

Set the default lookback window for logs/metrics queries.

Runs through the same parser every query uses (`timerange.parse_duration`),
so a bad value is rejected HERE, at write time, with the caller still looking
at what they typed — not on some later `logs query` that inherits it silently
and fails somewhere less obvious.

**Arguments:** `since` (required)

### `graf settings show`

Show every setting, its value, and where it came from.

