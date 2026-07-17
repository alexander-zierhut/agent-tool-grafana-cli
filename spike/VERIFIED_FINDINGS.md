# Grafana API — VERIFIED against the live instance
**Probed 2026-07-17 against `https://grafana.example.com` (Grafana 13.0.3) with a real service-account token.**
Trust this file over the docs. Every line below was observed, not read.

## Auth
- Token type: **service account** (`glsa_…`, 46 chars). API keys are deprecated since Grafana 9.1.
- Header: `Authorization: Bearer glsa_…`
- Get one at: `<server>/org/serviceaccounts` → Add service account → Add token (**shown once**).
- `GET /api/user` → 200 returns the SA identity:
  `{"id":0,"uid":"service-account:N","login":"sa-1-<name>","orgId":1,"isGrafanaAdmin":false,…}`
  - **`id` is 0 and `isGrafanaAdmin` is false for a service account** — do NOT use either to decide capability.
    Our token can still list datasources, because that needs the **org Admin role**, which is
    different from `isGrafanaAdmin` (server admin). Use `/api/access-control/user/permissions` to
    know what you can actually do, not the `isGrafanaAdmin` flag.
- `GET /api/health` → 200 `{"database":"ok","version":"13.0.3","commit":"NA"}` — **unauthenticated**, the
  right reachability/version probe (analogous to Drone's `/version`).

## Endpoint status (all observed live)
| endpoint | result |
| --- | --- |
| `GET /api/health` | 200, no auth needed. `{database, version, commit}` |
| `GET /api/user` | 200 — the SA identity |
| `GET /api/org` | 200 |
| `GET /api/access-control/user/permissions` | 200 — **the honest capability probe** |
| `GET /api/datasources` | 200 (needs `datasources:read` = org **Admin** by default) |
| `GET /api/datasources/uid/{uid}` | 200 |
| `GET /api/search?type=dash-db` | 200 — dashboards |
| `GET /api/v1/provisioning/alert-rules` | 200 — alert rules |
| `GET /api/annotations` | 200 |

## The datasources on this instance
```
uid=<loki-datasource-uid>  type=loki        name=Loki     <-- LOGS
uid=<prom-datasource-uid>  type=prometheus  name=Mimir
```

## THE CORE FEATURE: "what can I even get logs from?"
Two working paths to Loki. **Both verified.**

### A) Datasource proxy (simple, Loki-native shapes)
`/api/datasources/proxy/uid/{dsUid}/loki/api/v1/...` → then the normal Loki API:
- `GET .../labels` → 200 `{"status":"success","data":[...]}`
  **Live result: 4 labels — `hostname`, `job`, `service_name`, `systemd_unit`.**
- `GET .../label/{name}/values` → the values. Live cardinality (values themselves are real
  infrastructure and deliberately not reproduced here — see the gitignored `spike/local-instance.md`):
  - `service_name`: **1** value
  - `job`: **1** value
  - `hostname`: **21** values (one per host)
  - `systemd_unit`: **87** values (one per unit, e.g. `grafana.service`)

  The shape of that distribution is the design input: two labels are useless as filters (cardinality
  1), and the two that matter have 21 and 87 values. So `logs sources` must show **cardinality per
  label**, not just label names — "you can filter on `job`" is worthless when `job` has one value.
- `GET .../query_range?query={...}&start=&end=&limit=&direction=backward` → 200
  `{"status":"success","data":{"resultType":"streams","result":[{"stream":{...},"values":[[ns,line],…]}]}}`

  **Timestamps: Loki switches on DIGIT COUNT, not unit.** ~~Seconds silently return
  nothing~~ — *that earlier claim was WRONG, and a live test caught it.* Loki's
  `parseTimestamp` is `len(value) <= 10 → seconds, else nanoseconds`. Measured, one
  instant, four encodings:

  | encoding | digits | result |
  | --- | --- | --- |
  | seconds | 10 | ✅ works |
  | **milliseconds** | **13** | ❌ **read as nanos → 1970 → empty, `status: success`** |
  | microseconds | 16 | ❌ read as nanos → 1970 → empty |
  | nanoseconds | 19 | ✅ works |
  | (11-digit boundary) | 11 | ❌ empty — confirms the `<=10` rule exactly |

  So the trap is **milliseconds**, not seconds — and millis are the natural reach
  (`Date.now()`, `time.time()*1000`, Grafana's own UI). The failure is a `success`
  with an empty result: indistinguishable from "there are no logs".
  **We always send 19 digits** (`TimeRange.loki()`), the one encoding that cannot be
  misread. Regression-tested live in `test_loki_reads_timestamps_by_DIGIT_COUNT_not_unit`.
- Also available: `.../series`, `.../index/stats`, `.../query` (instant).

### B) `POST /api/ds/query` (modern, unified, but harder)
```json
{"queries":[{"refId":"A","datasource":{"type":"loki","uid":"<uid>"},
             "expr":"{systemd_unit=\"grafana.service\"}","queryType":"range","maxLines":2}],
 "from":"now-1h","to":"now"}
```
→ 200 `{"results":{"A":{"status":200,"frames":[{"schema":{...},"data":{...}}]}}}`
- Accepts **relative time** (`now-1h`) — nicer than the proxy's nanoseconds.
- Returns **Grafana dataframes**, not Loki streams: columnar `schema`/`data.values`, far more work to
  parse than path A. It also returns per-query `stats` (bytes/lines processed per second).
- **Recommendation: use the PROXY (A) for logs.** Loki's own JSON is simpler and stable. Keep
  `/api/ds/query` in mind only if we later need mixed/multi-datasource queries.

## Traps (each cost real time or would have)
1. **`detected_level` appears in query results but NOT in `/labels`.** Live proof: the labels endpoint
   returns 4 names, yet a returned stream carried
   `['detected_level','hostname','job','service_name','systemd_unit']`. Loki *derives* some labels at
   query time. So "what can I filter on" ≠ "what comes back", and a discovery command that only reads
   `/labels` is telling a partial truth. Say so.
2. **`/labels` and `/label/{n}/values` are TIME-BOUNDED.** They default to a recent window; a label
   that existed last week may be absent now. Pass `start`/`end` explicitly and report the window used,
   or "what can I get logs from" silently changes answer by time of day.
3. **Timestamps on the proxy path are read by digit count** (`<=10` = seconds, else
   nanos). **Milliseconds (13 digits) → a window in 1970 → empty result, no error.**
   Corrected 2026-07-17: the original note here said "seconds → nothing", which was
   wrong — a live test contradicted it. This is the second time on this project that
   a *written-down* finding beat a *re-measured* one and lost. Re-measure.
4. **`isGrafanaAdmin: false` on a token that CAN list datasources.** Server-admin ≠ org-admin. Never
   gate a CLI feature on that flag; use `/api/access-control/user/permissions`.
5. **Service account `id` is 0.** Don't key anything on it; use `uid` (`service-account:N`).
6. `GET /api/datasources` needs org **Admin**. A Viewer token can query a datasource but cannot
   enumerate → "what can I get logs from" is impossible for Viewers. `server doctor` must say this
   precisely rather than surfacing a bare 403.

## Bash gotcha (bit me while probing)
`UID` is a **readonly** shell variable (your uid, e.g. 1000). `UID=P8E8…` silently fails and you probe
`/uid/1000/`. Use `DSUID`. Cost: one confusing 404.

## Implications for the CLI
- The killer feature is **discovery**: `graf logs sources` = labels + values + which datasources are
  log-capable, in one command. Grafana's UI makes you click through Explore to learn this.
- The refused number here (per the family's killer-feature principle): Grafana/Loki will not tell you
  **"what can I get logs from?"** in one place. You must know to enumerate datasources, filter to
  log-capable types, then hit each one's label API. Derive it.
- Log payloads are **prose, not JSON** — same carve-out as drone's `log view`: document that stdout is
  text there.
- The real instance has ~87 units across ~21 hosts: a big surface, so `--fields`/`--limit`/`--tail`
  matter from day one, and `logs sources` must report cardinality rather than dumping every value.
