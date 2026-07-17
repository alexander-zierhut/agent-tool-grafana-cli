# Working agreement for this repo

For anyone changing `agent-tool-grafana-cli` — human or model. The family-wide
standard is `agent-tool-shared-cli`'s `BLUEPRINT.md`; this file is what is
specific to Grafana, plus the scars.

## The one rule

**Reading is not observation.** Every non-obvious claim in this codebase was
measured against a live Grafana, and the ones that were not have already bitten
us. `spike/VERIFIED_FINDINGS.md` records what was measured. It is more
trustworthy than Grafana's docs — and less trustworthy than a fresh measurement:

> The findings file confidently stated "Loki timestamps are nanoseconds; seconds
> silently return nothing." **That was wrong.** Loki switches on *digit count*
> (`<=10` → seconds, more → nanoseconds), so seconds work fine and it is
> **milliseconds** that land in 1970 and return an empty success. A live test
> written to confirm the note contradicted it instead.

So: when a finding matters, re-measure it. Write the test that would fail if the
note were wrong.

## Layering

```
spec/errors/config          identity, exit codes, profiles       (no HTTP)
timerange/loki/routing/     PURE LOGIC — the testable heart       (no HTTP)
  analysis
client                      HTTP: auth, org header, retries, error mapping
sources                     discovery (thin: client in, data out)
commands/*                  argv -> pure logic -> Emitter
```

`client.py` must not import a command module. Pure logic must not import
`client`. That layering is why ~264 hermetic tests run in two seconds with no
server, and it is not negotiable — the moment query-building needs a socket, it
stops being tested.

## Non-negotiables

- **All output goes through the Emitter.** `obj.emitter.emit(data, columns=[...])`.
  Never `print()` a payload; never hand-build JSON. `emitter.message()` is
  table-mode-only prose.
- **Never invent an exit code.** 0–7 are the family contract; 8+ are ours and are
  added only for a condition someone has *observed*. Renumbering is a breaking
  change to an API that agents branch on.
- **`NotFoundError` and `ApiError` are SIBLINGS.** An `except ApiError` ladder
  looks exhaustive and is not. **End every ladder in `except OpError`.**
- **Never declare a reserved flag** (`--format/-f`, `--output/-o`, `--fields`,
  `--columns`, `--dry-run`, `--stream`, `--no-context`). They are stripped from
  argv before Click parses, so a command declaring one could never receive it.
  File destinations are **`--out`**. `tests/test_globals_unit.py` enforces this
  tree-wide.
- **Writes go through `client.post/put/patch/delete`** so `--dry-run` is
  intercepted in the transport and no command can bypass it.
- **Observations are not failures.** "The project is unhealthy", "this alert
  reaches nobody", "a target is down" are things the CLI *succeeded* at finding
  out. Exit 0. Gate the non-zero behind an explicit `--exit-code`, in a band far
  from the error codes (20+).
- **Never state a count you have not counted.** Counts rot on every commit; if you
  quote one, name its basis and how you got it.
- **Never commit a real hostname, datasource uid, token or org name.** They live
  in `.env` and `spike/local-instance.md`, both gitignored. `.gitignore` comes
  before the first secret-adjacent file, not after.

## Grafana-specific things that will bite you

- **Tokens are hard-scoped to one org.** Multi-org is one profile per org. The
  client sends `X-Grafana-Org-Id` as an *assertion* — it cannot widen a token, it
  just turns a silently-wrong-org into a loud 401 (exit 9).
- **Datasource uids are per-org.** Never cache one across profiles. The sticky
  context is keyed by profile precisely because of this.
- **Always tunnel through the datasource proxy.** One org's Loki datasource
  injects a secret header held write-only in `secureJsonFields`; the CLI can never
  read it, so it can never talk to Loki directly — and a direct connection would
  silently skip the tenant scoping and return another tenant's logs.
- **`detected_level` is derived at query time.** Legal in a pipeline stage, dead
  in a selector. `loki.build_query` places it; do not hand-roll it.
- **Label APIs are time-bounded.** Every payload that used a window must embed
  `window.describe()`. A result without its window is half an answer.
- **The datasource proxy can 502 with an empty body.** That is exit 8, not an API
  error, and it is not retried: Grafana answered, so the 502 is a fact about the
  backend, not a blip.
- **A permission name is not a capability** — the scope decides. Report scopes.
- **Two endpoints disagree about contact points.** Read both.
- Fan-out commands (`scan`, `logs sources`, `datasource test`) **capture per-item
  errors into the payload** rather than raising. One dead backend must never blank
  a report.

## Tests

```bash
make test-unit     # hermetic (268 tests, ~2s); the marker, never a file list
make stack         # boot Grafana + Loki + Prometheus
make test          # boots + seeds + runs EVERYTHING (321 tests)
make stack-down    # tear it down, volumes and all
make docs          # regenerate docs/COMMANDS.md; CI fails if it drifts
```

**Test against our own stack, never against somebody's production.** This is the
rule that shapes the rest. An earlier CI pointed at the maintainer's real Grafana
behind repository secrets: it could not run on a fork's PR, could only ever read,
and "passed" by skipping everywhere else — while the CLI's whole write surface
(create a dashboard, create an alert rule, watch it fire into a receiver that
cannot deliver) went untested. `docker-compose.yml` + `scripts/bootstrap_test_stack.sh`
boot and seed a throwaway stack in ~30s with no credentials.

- **The write tier is interlocked.** Destructive tests require
  `GRAFANA_ALLOW_WRITES=1`, which only the bootstrap script sets, and it only ever
  talks to a throwaway stack. Point the suite at a real Grafana and the write
  tests cannot run. There is a meta-test asserting the interlock is armed — an
  interlock nobody checks is decoration, and it has already caught one real
  failure (`_hermetic` stripped `GRAFANA_URL` while leaving the interlock on, so
  the whole live suite skipped itself and went green).
- **`conftest._hermetic` strips the environment, so live tests must read
  `conftest.live_env`,** which is a snapshot taken before the fixture ran.
  `os.environ["GRAFANA_URL"]` is empty inside any test body.
- The hermetic tests use a **hand-rolled fake client**, not a transport mock. A
  transport mock tempts you into simulating the API, and the API is wrong in ways
  you would encode wrongly. What the server really does belongs in the live tier,
  where the server can contradict you — and has: three separate claims in this
  repo were disproved by tests written to confirm them.
- **The seeded stack is deliberate, not arbitrary.** Label cardinality is 3/2/1 so
  `logs sources` has a useless label to report; ids in the seeded logs are non-hex
  because Docker Swarm ids broke fingerprinting for real; one datasource points at
  a dead port so exit 8 is proven by a real 502.
- **A doc that names a command is tested against the real tree**
  (`tests/test_guide_unit.py`). Prose makes promises nothing executes: a sibling
  shipped a `guide` advertising a command that never existed, and a SKILL.md
  pointing at a `gotchas` topic that 404'd — the most inviting name on the list,
  so it was the first thing an agent tried.

## Scars worth knowing (each cost real time)

- `--output` was eaten as a format flag in a sibling tool for four releases;
  `attach download --output f.pdf` wrote to the CWD, exit 0. Hence `--out`.
- `Emitter` accepted `columns=["a","b"]` in its type alias and crashed on it in
  table/csv/markdown while working in json. Two implementers hit it the same
  afternoon. Fixed in the chassis by widening the input — an API that is easy to
  hold wrong will be held wrong.
- `\berror\b` does not match "errors". The classifier silently under-claimed until
  a test caught it. Under-claiming is the worse direction for a tool whose job is
  finding problems.
- A live `scan` reported **204 distinct problems out of 256 lines** that were all
  one problem: every line carried a Docker Swarm `task.id`, and those ids are
  base32-ish, not hex, so the `<HEX>` rule could not touch them. Modern
  orchestrators do not emit hex ids. Hence `<ID>`.
- **"Listing datasources needs org Admin"** was written in the findings file, the
  README, the guide, the `auth login` prompt and the 403 hint. It was reasoned,
  never measured, and **false** — a Viewer enumerates fine; only writes need
  Editor. It survived a spike, a review and six agents, and it was talking users
  into granting a CLI org-administration rights it never uses. One
  `docker compose up` disproved it. Over-privilege by documentation is a security
  bug. The matrix is now `tests/test_roles_live.py`, not prose.
- **The derived level label is called `level` on Loki 3.0 and `detected_level` on
  newer builds.** Hardcoding either returns an empty *success* on the other. We
  union both. Found only because CI boots a Loki that differs from production.
- Service-account **token names must be unique per ORG**, not per account
  (`serviceaccounts.ErrTokenAlreadyExists`). The bootstrap silently emitted empty
  tokens until it started failing loudly.
- `UID` is a **readonly** shell variable. Probing `/uid/$UID/` silently asks about
  uid 1000. It is documented in the findings file and it still caught the author
  a second time. Use another name.
