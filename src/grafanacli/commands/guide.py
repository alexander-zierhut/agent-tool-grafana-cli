"""`graf guide` — the built-in operating manual.

The point is self-sufficiency: an agent with only this binary and no other
context can run `graf guide` and learn the output contract, how to authenticate,
the domain model, and the gotchas — with no README, no network and no config. It
must therefore be structurally impossible for this command to fail with a config
or auth error, or to block on a prompt.

Verify that property, do not assume it:

    env -i PATH=/usr/bin:/bin HOME=/nonexistent ./graf guide   # -> exit 0

Every gotcha below was VERIFIED against a live Grafana 13.0.3 during a spike.
They are not folklore; they are the things that will otherwise cost an agent a
wrong answer — or, worse, a confidently wrong one.
"""

from __future__ import annotations

import typer

OVERVIEW = """\
graf — operating guide (run `graf guide <topic>` for details)

WHAT IT IS
  A CLI for Grafana, built for AI agents: discover what you can get logs from,
  read them, work out what is wrong, and set up an alert so you hear about it
  next time. Loki for logs, Prometheus/Mimir for metrics.

  This is NOT `grafana-cli` (the plugin tool shipped with every Grafana server)
  and NOT `grafanactl` (Grafana's dashboards-as-code tool). All three can coexist.

OUTPUT CONTRACT (important for scripting/agents)
  - stdout is JSON by default — parse it.
  - Errors go to stderr as JSON with a non-zero exit code: {"error": "...", "status": 404}.
  - EXCEPTION: `logs query --raw` prints raw log TEXT, not JSON. Logs are prose.
  - Exit codes: 0 ok · 1 generic · 3 config · 4 auth · 5 not-found · 6 conflict ·
                7 validation · 8 datasource-unreachable · 9 wrong-org · 130 interrupted
  - Change format anywhere on the line: `-o table`, `-o markdown`, `-o csv`.
  - Trim output: `--fields label,values`. Big lists: `--stream` (NDJSON).
  - PREVIEW a write without doing it: add `--dry-run` — prints the request, exits 0.
  - Observations never become exit codes. "The project is broken" and "an alert
    reaches nobody" are FINDINGS, reported with exit 0. Opt in with `--exit-code`
    (which uses 20, far from the error band) if you want to branch on them.

AUTHENTICATE
  Interactive:      graf auth login          (asks for the URL, then shows you
                                              exactly where to get a token)
  Non-interactive:  export GRAFANA_URL=https://grafana.example.com
                    export GRAFANA_TOKEN=glsa_xxxxxxxx
  Get a token:      <your-grafana>/org/serviceaccounts  → Add service account →
                    Add token (shown ONCE). Give it the **Admin** role.
  Check:            graf auth status         (says WHICH backend/token is in use)

  Why Admin: listing datasources needs `datasources:read`, which is org Admin by
  default. An Editor token can query a datasource it already knows the uid of but
  cannot ENUMERATE — so `graf logs sources`, the thing you start with, cannot
  work. `graf server doctor` reports exactly what your token can do.

  NOTE: GRAFANA_TOKEN in the environment OVERRIDES a keyring login, silently. If
  results look wrong, run `auth status` — it names the token actually in use.

THE ONE THING TO KNOW: DISCOVER BEFORE YOU QUERY
  You cannot query what you cannot name, and Grafana will not tell you what
  exists. Nothing in the UI or the API answers "what can I get logs from?" — you
  are expected to already know. So start here, always:

    graf logs sources          # which datasources carry logs, which labels they
                               # have, and HOW MANY VALUES each label has

  That last part is the point. A label with one value cannot narrow anything
  down; listing it as a filter you "can use" is noise. `sources` counts.

THE WORKFLOW THIS IS BUILT FOR
    1. deploy something (e.g. `drone-cli wait --commit HEAD`)
    2. graf scan --since 15m              # is it broken? what should I look at?
    3. graf logs similar "<a line>"       # has this happened elsewhere/before?
    4. graf alert create --title ... -q '<query>'
       → and it tells you AT CREATION TIME whether that alert would reach anyone

ORGANISATIONS: ONE PROFILE PER ORG
  A service-account token is hard-scoped to exactly ONE org. There is no header
  or flag that widens it — asking for another org is a 401 (exit 9). So:

    graf auth login                     # org A -> profile "default"
    graf auth login --profile sales     # org B -> profile "sales"
    graf -p sales logs sources

  Datasource uids are PER-ORG: the same Loki has a different uid in each. Sticky
  defaults (`graf context`) are therefore stored per profile.

KEY GOTCHAS (each verified live — save yourself a wrong answer)
  - `detected_level` is NOT an indexed label. It does not appear in the label
    list, yet it filters — Loki derives it at query time. Use `--level error`;
    `{detected_level="error"}` matches NOTHING.
  - Label lists are TIME-BOUNDED. "What can I get logs from" has a different
    answer at 09:00 and 17:00. Every payload here carries the window it used.
  - Loki reads timestamps by DIGIT COUNT: <=10 digits = seconds, more =
    nanoseconds. So MILLISECONDS (13 digits) are read as nanos, land in 1970, and
    return success with an empty result. This CLI always sends 19 digits.
  - A datasource can be configured and its BACKEND still be down: that is exit 8
    (datasource-unreachable), not an auth or config problem. Grafana is fine; the
    thing behind the datasource is not.
  - An alert can fire forever and notify NOBODY, and every Grafana screen shows
    it working. Run `graf alert route <uid>` before you trust an alert.
  - Contact points: two endpoints disagree. A receiver with zero integrations
    shows up in one and not the other. `graf notify list` reads both.
  - Service accounts report `id: 0` and `isGrafanaAdmin: false` even when they
    ARE an org Admin. Never gate on those. Use `graf server doctor`.
  - A permission NAME is not a capability — the SCOPE matters. A token can hold
    `orgs:read` and still be refused.

DISCOVER
  graf logs sources        what can I get logs from? (start here)
  graf logs search <term>  which source is my thing called?
  graf datasource list     everything, of every type
  graf server doctor       what is my token actually allowed to do?
  graf guide <topic>       the topics below

TOPICS:  logs · scan · similar · alerts · notify · metrics · dashboards · orgs ·
         output · auth · context · settings · gotchas · workflow
"""

TOPICS: dict[str, str] = {
    # ---------------------------------------------------------------
    "logs": """\
LOGS (Loki)

  DISCOVER FIRST. You cannot query what you cannot name.

    graf logs sources                    # datasources + labels + CARDINALITY
    graf logs sources --since 24h        # labels are time-bounded; widen to see more
    graf logs search api                 # which label value contains "api"?
    graf logs search api --content       # ...and which logs mention it

  THEN QUERY. Build a selector from labels, or pass raw LogQL.

    graf logs query --label systemd_unit=docker.service --since 15m
    graf logs query -l hostname=~web.* -l systemd_unit=api.service
    graf logs query --level error --since 1h
    graf logs query --contains timeout --exclude healthcheck
    graf logs query --regex 'conn.*refused'
    graf logs query -q '{job="x"} |= "boom"'        # raw LogQL, used verbatim
    graf logs query --raw                            # bare log TEXT, not JSON

  Matcher operators go on the VALUE:
    -l host=web1        exact        -l host=~web.*      regex
    -l host=!web1       not equal    -l host=!~web.*     not regex

  Every result echoes the LogQL actually sent, under "query". Iterate on it.

  LEVELS AND WHERE TO LOOK
    graf logs levels                     # level distribution per source
    graf logs tail --label unit=api.service   # follow (POLLING -- see `guide gotchas`)

  LIMITS
    --limit defaults to your `settings show` value (100). The instance this was
    built against has ~87 units across ~21 hosts; unbounded output is a
    context-window accident, not thoroughness.
""",
    # ---------------------------------------------------------------
    "scan": """\
SCAN — "does this work? any irregularities?"

  One pass that answers the question you actually have after a deploy.

    graf scan                            # the default window (settings: 1h)
    graf scan --since 15m                # just since my deploy
    graf scan -l systemd_unit=api.service    # scope it to "my project"
    graf scan --category deprecation     # only one kind of finding
    graf scan --exit-code                # exit 20 if unhealthy (for CI)

  WHAT IT DOES
    Queries for problem lines two ways -- by Loki's own `detected_level`, and by
    regex for the high-severity things a mis-assigned level would miss (panics,
    OOM kills, disk-full) -- then COLLAPSES them by fingerprint. Ten thousand
    lines are not ten thousand problems; they are usually a dozen problems
    repeated. Each finding carries a count, how many sources it hit, an example
    line, and a `next` command.

  HOW TO READ IT
    verdict.healthy is CONSERVATIVE: false whenever anything matched at all. A
    tool that says "looks fine" while a panic sits in its own output has misled
    you. Findings are ranked severity-first, then volume: one panic outranks a
    thousand timeouts, because the panic is the thing to go and fix.

  WHAT IT CANNOT DO
    - The classifier is a HEURISTIC over prose. A line saying "no errors found"
      contains "error" and will be flagged. Every finding carries its raw line
      so you can overrule it. Categories rank output; they are not verdicts.
    - It reads LOGS ONLY. For the metrics side: `graf metrics up`.
    - A service that logs NOTHING looks identical to a healthy one from here.
      Silence is not health.
""",
    # ---------------------------------------------------------------
    "similar": """\
FINDING SIMILAR PROBLEMS — the methodology

  The question "has this happened before / somewhere else?" is answered by
  FINGERPRINTING: strip the variable parts out of a line and search for the shape.

    connection to 10.0.0.7:5432 failed after 1.2s
    connection to 10.0.0.9:5432 failed after 0.4s
      -> both fingerprint to:  connection to <ADDR> failed after <DUR>

  So one line becomes a query that finds its siblings:

    graf logs similar "connection to 10.0.0.7:5432 failed after 1.2s"
    graf logs similar --from-last              # take the most recent error
    graf logs similar "<line>" --since 7d      # has it happened before this week?

  The output tells you WHICH sources it appears on and how often. That is the
  distinction that matters and that a flat log tail hides: "this error is on one
  host" and "this error is on all twenty-one" are completely different problems.

  Placeholders the fingerprinter uses: <TS> <UUID> <ADDR> <HEX> <DUR> <SIZE> <N>.
  The same collapse powers `graf scan`, which is just this run over a corpus
  instead of one line.

  WIDER METHODOLOGY
    1. graf scan --since 1h                    # what is broken?
    2. graf logs similar "<the example line>" --since 7d
                                               # new, or chronic?
    3. graf logs levels                        # is it isolated or everywhere?
    4. graf metrics up                         # is anything actually down?
    5. graf alert create ...                   # so it tells you next time
""",
    # ---------------------------------------------------------------
    "alerts": """\
ALERT RULES

    graf alert list
    graf alert get <uid>
    graf alert firing                    # what is going off right now
    graf alert route <uid>               # WILL IT ACTUALLY REACH ME?
    graf alert create --title "API errors" -q '<query>' --folder <uid>
    graf alert delete <uid> --yes

  `alert route` IS THE POINT. Grafana will not tell you, anywhere, whether a rule
  that fires will reach a human. It gives you the rule, the policy tree and the
  contact points, and joins none of them. So an alert can fire forever into a
  receiver with zero integrations while every screen shows it firing normally.
  (That is not hypothetical: it was the live state of the instance this was built
  against -- nine alerts, all routed to a receiver with no integrations.)

    graf alert route <uid>                       # for an existing rule
    graf alert route --label severity=critical   # hypothetical: what WOULD happen?

  `alert create` runs that check automatically and includes it in its output --
  creating an alert nobody will hear is the exact failure this tool prevents.

  CREATING
    - Rule permissions are FOLDER-SCOPED. Your token may create rules in one
      folder and not another; `graf server doctor` lists which. A create into the
      wrong folder is a 403, not a validation error.
    - Use --dry-run first: it prints the exact request without sending it.
    - `for` (pending period) defaults to 5m: how long the condition must hold
      before it fires. 0 means "fire on a single bad evaluation" -- usually noise.
""",
    # ---------------------------------------------------------------
    "notify": """\
NOTIFICATIONS — "if that happens again, let me know"

    graf notify list                     # contact points, and whether they WORK
    graf notify policies                 # the routing tree, flattened
    graf notify check                    # audit EVERY rule's delivery, one pass
    graf notify test <name>              # send a real test notification
    graf notify silences

  THE TRAP THIS GROUP EXISTS FOR
    A "contact point" is really a set of integrations. A receiver can exist with
    an EMPTY set: named, referenced by the policy tree, routed to -- and unable
    to deliver anything. Grafana shows nothing wrong.

    Worse, two endpoints disagree about them: the provisioning API lists contact
    points, the alertmanager API lists receivers, and a hollow receiver appears in
    the second and not the first. `notify list` reads BOTH and marks anything
    unusable. Do not trust a "contact points: []" from anywhere else.

  HOW ROUTING ACTUALLY WORKS (Alertmanager semantics, ported exactly)
    - The tree is walked top-down; a node participates only if ITS matchers pass.
    - Children are tried IN ORDER; the first match wins and the walk STOPS --
      unless that child sets `continue: true`, which keeps siblings in play. That
      is how one alert reaches two receivers.
    - If no child matches, the current node's receiver applies.
    - A missing label counts as the EMPTY STRING. So `severity!="critical"`
      matches an alert with no severity at all.
    - A child with no receiver INHERITS its parent's.
    - Grafana injects `alertname` and `grafana_folder` itself; the default policy
      groups by exactly those two.

    graf notify check    # runs all of that against every rule and reports the
                         # ones that reach nobody
""",
    # ---------------------------------------------------------------
    "metrics": """\
METRICS (Prometheus / Mimir)

    graf metrics up                      # what is DOWN right now (start here)
    graf metrics list --filter http      # which metrics exist? (841 on a real box)
    graf metrics list --describe         # ...with type and help text
    graf metrics describe <metric>       # what is it, and how do I slice it?
    graf metrics labels --label job
    graf metrics query -q 'rate(http_requests_total[5m])'
    graf metrics query -q 'up' --range --since 1h --step 60

  NOTES
    - Mimir, Thanos and Cortex all speak PromQL; they are `prometheus` type
      datasources and this all works against them unchanged.
    - Timestamps here are SECONDS. Loki's are nanoseconds. Both go through
      `--since`/`--from`/`--to`, so you never touch either -- but if you use
      `graf raw`, you do.
    - `--range` returns a matrix (a series of points); without it you get an
      instant vector (one value per series). Ask for a range only if you need the
      shape over time; an instant is far cheaper.
    - `--step` is computed from the window if you omit it. Prometheus refuses
      more than 11000 points per query.
""",
    # ---------------------------------------------------------------
    "dashboards": """\
DASHBOARDS

    graf dashboard list
    graf dashboard search latency
    graf dashboard get <uid> --out dash.json     # NOTE: --out, not --output
    graf dashboard panels <uid>                  # what does it actually QUERY?
    graf dashboard create --title "API" -q '<query>' -d <datasource>
    graf dashboard create --file dash.json --folder <uid>
    graf dashboard folders

  `dashboard panels` is the useful one: it extracts every panel's queries,
  including the ones nested inside collapsed rows, and hands you LogQL/PromQL you
  can run yourself with `graf logs query -q` / `graf metrics query -q`. Someone
  else's dashboard is a pile of queries somebody already debugged; this is how you
  reuse them instead of reinventing them.

  WRITING
    - `--out` writes a file. `--output` is RESERVED as a global format flag and
      would be eaten before the command sees it (see `guide output`).
    - Dashboards use optimistic locking on `dashboard.version`. Saving a stale
      one is a 412 -> exit 6 (conflict). `--overwrite` is the way through.
    - `/api/folders` includes a VIRTUAL folder ("Shared with me", id -1). It is
      not a real folder and cannot hold anything.
""",
    # ---------------------------------------------------------------
    "orgs": """\
ORGANISATIONS — one profile per org

  A service-account token belongs to exactly ONE org, permanently. Verified live,
  both directions: send `X-Grafana-Org-Id` for any other org and you get

    401 {"messageId": "api-key.organization-mismatch"}   -> exit 9

  There is no flag, header or endpoint that widens a token. So multi-org is done
  with PROFILES, each holding its own URL, token and org:

    graf auth login                      # org A -> profile "default"
    graf auth login --profile sales      # org B -> profile "sales"
    graf auth profiles                   # what have I got?
    graf -p sales logs sources           # use one
    graf org current                     # which org am I in right now?
    graf org check                       # does my profile's org match my token's?

  CONSEQUENCES THAT BITE
    - Datasource UIDs are PER-ORG. The same logical Loki has a different uid in
      each org. Never copy a uid between profiles; never cache one.
    - Sticky context is therefore stored PER PROFILE. `graf context set
      --datasource X` in one profile does not leak into another.
    - `graf org list` shows the orgs you have PROFILES for. That is the honest
      answer for a normal token: listing all orgs on the server needs a
      server-admin token (`/api/orgs` -> 403 otherwise).
    - Exit 9 (wrong org) is NOT exit 4 (bad auth). Re-running `auth login` with
      the same token cannot fix it -- you need a token minted in that org.
""",
    # ---------------------------------------------------------------
    "output": """\
OUTPUT & THE RESERVED FLAGS

  FORMATS (work anywhere on the line, before or after a subcommand)
    -o json       default; parse this
    -o table      human
    -o markdown   for a PR or a doc
    -o csv        for a spreadsheet
    --fields a,b  keep only these keys
    --stream      NDJSON, one object per line, for big results

  RESERVED — no command may declare these, they are stripped before parsing:
    --format -f --output -o --fields --columns --dry-run --stream --no-context

  Which is why every file destination in this tool is `--out`:

    graf dashboard get <uid> --out dash.json     # correct
    graf dashboard get <uid> --output dash.json  # would be read as a FORMAT

  A sibling tool shipped that exact bug for four releases: the path was swallowed
  as a format, the format silently fell back to json, and the file was written to
  the working directory under a different name -- exit 0, no error. A tree-wide
  test now makes it impossible here.

  EXIT CODES
    0 ok (including a successful --dry-run)     6 conflict (e.g. dashboard 412)
    1 generic                                   7 validation
    3 config (no profile/token)                 8 datasource backend unreachable
    4 auth (bad/insufficient token)             9 wrong org for this token
    5 not found                                 130 interrupted

  Codes 0-7 mean the same thing across every tool in this family. 8+ are ours.

  OBSERVATIONS ARE NOT FAILURES
    "The project is unhealthy" and "this alert reaches nobody" are things the CLI
    successfully FOUND OUT. They exit 0. Add `--exit-code` to `scan`, `alert
    route`, `notify check` or `metrics up` to opt into exit 20 instead.
""",
    # ---------------------------------------------------------------
    "auth": """\
AUTHENTICATION

  INTERACTIVE
    graf auth login              # asks for the URL, then shows you where to get
                                 # a token for THAT server, then verifies it
    graf auth login --profile sales      # a second org

  NON-INTERACTIVE (CI)
    export GRAFANA_URL=https://grafana.example.com
    export GRAFANA_TOKEN=glsa_xxxxxxxx
    # that is enough -- no config file needed at all

  WHERE THE TOKEN LIVES
    Precedence: environment > OS keyring > 0600 file. `graf auth status` always
    names WHICH one spoke. That matters: an exported GRAFANA_TOKEN silently
    overrides a keyring login, and the fix is visibility, never inverting the
    order (CI depends on env winning).

  WHICH TOKEN TO MINT
    <your-grafana>/org/serviceaccounts -> Add service account -> Add token.
    The value is shown ONCE. Give it the **Admin** role: listing datasources needs
    `datasources:read`, which is org Admin by default. An Editor token can query a
    datasource whose uid it already knows, but cannot enumerate -- so `logs
    sources` is impossible and the tool loses its point.

    API keys (/org/apikeys) are deprecated since Grafana 9.1. Use a service
    account.

    graf server doctor    # tells you exactly what YOUR token can do, with scopes

  THINGS THAT LOOK LIKE AUTH BUGS AND ARE NOT
    - `id: 0` and `isGrafanaAdmin: false` are NORMAL for a service account, even
      an Admin one. Server-admin != org-admin. Never gate on either.
    - Exit 9 means the token is VALID but for another org. `auth login` again
      will not help; mint a token in the org you want.
    - Holding a permission NAME is not capability -- the SCOPE matters.
""",
    # ---------------------------------------------------------------
    "context": """\
STICKY CONTEXT — defaults that persist across commands

    graf context set --datasource <uid>      # stop typing -d every time
    graf context set --since 15m
    graf context show                        # and WHICH profile it belongs to
    graf context clear
    graf --no-context logs query ...         # ignore it for one command

  Keys: datasource, since, folder. Each matches a real option on real commands
  (a test enforces that -- otherwise renaming an option would silently turn its
  context key into a no-op, and you would keep setting it forever with no effect).

  PER PROFILE, NOT GLOBAL — this is the important part.
  Datasource uids are per-org. A single global sticky `--datasource` would resolve
  to a uid that does not exist the moment you switch profile, and Grafana answers
  that with a 404 that blames the datasource rather than your config. So each
  profile carries its own context, and they never leak into each other.

  `auth`, `server`, `org`, `settings`, `guide`, `install` and `context` itself
  ignore the sticky context on purpose: they are what you run to diagnose things,
  and a saved default silently rescoping a diagnostic is the last thing you want
  when you are already lost.
""",
    # ---------------------------------------------------------------
    "settings": """\
SETTINGS

    graf settings show
    graf settings set-format table       # default output format
    graf settings set-since 15m          # default lookback for logs/metrics/scan
    graf settings set-limit 200          # default result cap
    graf settings path                   # where the config file lives

  Every setting has a sane default or is asked once on first run; there is no
  silent half-configured state. Defaults: format json (asked once on a TTY),
  since 1h, limit 100.

  `set-since` is validated when you set it, not when a query later fails: a bad
  duration is rejected at write time.

  The config file holds NO secrets. Tokens live in the OS keyring (or a 0600 file
  if no keyring is available).

  RELOCATE EVERYTHING
    export GRAFANACLI_CONFIG_DIR=/some/path      # config + fallback credentials
""",
    # ---------------------------------------------------------------
    "gotchas": """\
GOTCHAS — all verified live against Grafana 13.0.3

  QUERYING LOGS
  - `detected_level` is NOT an indexed label. It is absent from the label list and
    it still filters, because Loki derives it at query time. So it is legal in a
    PIPELINE stage and matches nothing in a SELECTOR:
        {job=~".+"} | detected_level="error"     works
        {detected_level="error"}                 matches NOTHING
    Use `--level error` and let the CLI place it.
  - Label lists are TIME-BOUNDED. A label whose streams went quiet drops out of
    the answer, so "what can I get logs from" changes by time of day. Every
    payload carries the window it used. Widen with `--since 24h` if something you
    expected is missing.
  - Loki reads timestamps by DIGIT COUNT, not unit: <=10 digits = seconds, 11+ =
    nanoseconds. MILLISECONDS (13 digits) are therefore read as nanoseconds, land
    in 1970, and return {"status":"success"} with an empty result -- no error, and
    indistinguishable from "there are no logs". This CLI always sends 19 digits;
    if you use `graf raw`, you must too.
  - Loki rejects an empty `{}` selector. "Everything" must be spelled with a real
    matcher, e.g. `{hostname=~".+"}` -- and that only returns streams that CARRY
    the label, so a rare label silently drops streams that lack it.
  - `logs tail` POLLS; it is not a live stream (Loki's real tail is a WebSocket
    and we will not take that dependency). A burst faster than the interval can
    be missed if it exceeds --limit.

  ALERTING
  - An alert can fire forever and notify NOBODY: a receiver with zero
    integrations is valid, routable, and silent. Every Grafana screen shows it
    working. `graf alert route <uid>` is the only way to know.
  - Two endpoints disagree about contact points. The provisioning API lists
    contact points; the alertmanager API lists receivers; a hollow receiver
    appears in the second and not the first. `graf notify list` reads both.
  - Alert-rule permissions are FOLDER-SCOPED. A token may write rules in one
    folder and not another. `graf server doctor` lists which.
  - `/api/alertmanager/grafana/config/api/v1/alerts` is 403 even for an org Admin
    (it needs `alert.notifications.config-history:read`). Not a bug; do not use it.

  IDENTITY & ORGS
  - Service accounts report `id: 0` and `isGrafanaAdmin: false` even when they are
    an org Admin. Server-admin != org-admin. Never gate on either flag.
  - A permission NAME is not a capability -- the SCOPE decides. A token can hold
    `orgs:read` scoped to nothing and still be refused by /api/orgs.
  - Tokens are hard-scoped to one org (exit 9 if you ask for another). Datasource
    uids differ per org. One profile per org.
  - `/api/user/orgs` returns 304 with an EMPTY BODY for a service account. It is
    useless; `graf org list` shows your profiles instead.

  DATASOURCES
  - A datasource can be configured and its backend still be DOWN: exit 8, not an
    auth or config error. The proxy returns 502 with an EMPTY body, so anything
    assuming JSON dies rather than reporting it.
  - Listing datasources needs org Admin. An Editor token can query one it already
    knows and cannot discover any. That kills `logs sources`.
  - Some datasources inject SECRET headers (held write-only in Grafana's
    `secureJsonFields`) to scope a tenant. You can never read them, so you can
    never talk to Loki directly and reproduce the request -- and a direct
    connection would silently skip the scoping. Always go through the proxy.

  OUTPUT
  - `--output` is a FORMAT flag, globally. File destinations are `--out`.
  - Findings are not failures. `scan` exits 0 on a broken project because it
    succeeded at scanning. Use `--exit-code` to branch on the observation.
""",
    # ---------------------------------------------------------------
    "workflow": """\
THE WORKFLOW THIS TOOL IS FOR

  You just deployed. Something feels wrong. Fifteen seconds, start to finish:

    1. WHAT AM I EVEN LOOKING AT?
         graf logs sources
       Labels with one value are useless as filters; the report says which.

    2. IS IT BROKEN?
         graf scan --since 15m
       Errors, panics, OOMs and deprecations, collapsed by fingerprint and ranked
       severity-first. Each finding carries a `next` command.

    3. IS THIS NEW?
         graf logs similar "<the example line>" --since 7d
       Same shape, different ids. Tells you if you just caused it, and whether it
       is on one host or all of them.

    4. IS ANYTHING ACTUALLY DOWN?
         graf metrics up

    5. TELL ME NEXT TIME
         graf alert create --title "..." -q '<the query>' --folder <uid> --dry-run
         graf alert create --title "..." -q '<the query>' --folder <uid>
       The create reports whether that alert would reach a human. If it says no:
         graf notify list        # which contact points can actually deliver?
         graf notify check       # which existing rules are already silent?

  ACROSS TOOLS (the family)
    drone-cli wait --commit HEAD && graf scan --since 10m
      -> "my commit built; is it healthy in production?"

  WHEN LOST
    graf server doctor      # is it me, my token, my org, or the datasource?
    graf guide gotchas      # the things that will otherwise cost you a wrong answer
""",
}


def guide(
    topic: str = typer.Argument(
        None,
        help="A topic to expand. Omit for the overview; `graf guide topics` lists them.",
    ),
) -> None:
    """Built-in operating guide — how to use this CLI without external docs.

    Deliberately does NOT take a `typer.Context` and never builds an AppContext:
    this must work with no config, no token, no network and no TTY. It is what an
    agent runs FIRST, and what a human runs when everything else is broken —
    so it cannot be allowed to fail for the same reasons everything else did.

    Prints with `typer.echo` rather than the Emitter, on purpose: this is prose
    for a human or an LLM, not a payload. There is nothing here to `--fields`.
    """
    if topic is None:
        typer.echo(OVERVIEW)
        return

    key = topic.strip().lower()
    if key in ("topics", "list"):
        typer.echo("\n".join(sorted(TOPICS)))
        return

    body = TOPICS.get(key)
    if body is None:
        # Never a bare "unknown topic": the whole point of this command is that it
        # is the way out of not knowing, so a dead end here is worse than useless.
        typer.echo(
            f"No topic {topic!r}. Available topics:\n  " + "  ".join(sorted(TOPICS)) + "\n\n"
            "Run `graf guide` for the overview.",
            err=True,
        )
        raise typer.Exit(2)
    typer.echo(body)
