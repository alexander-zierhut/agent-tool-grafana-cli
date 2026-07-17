#!/usr/bin/env bash
# Seed the throwaway Grafana stack and print the env the tests need.
#
#   docker compose up -d && ./scripts/bootstrap_test_stack.sh
#   eval "$(./scripts/bootstrap_test_stack.sh --export)"
#   pytest
#
# WHY THIS EXISTS. `grafana-cli`'s whole surface is writes and multi-org: create a
# dashboard, create an alert rule, discover that a contact point cannot deliver,
# refuse to cross an org boundary. None of that is testable against somebody's
# production Grafana, so without this script the write half of the CLI is tested
# by `--dry-run` and optimism.
#
# EVERY SEQUENCE BELOW WAS PROBED AGAINST A REAL GRAFANA 13.0.3 BEFORE BEING
# WRITTEN DOWN. The notable one: `X-Grafana-Org-Id` **works** for a basic-auth
# admin (it switches org) and **401s** for a service-account token (which is
# hard-scoped to one org). That asymmetry is what lets this script seed two orgs
# with one admin credential, and it is also the fact the CLI's whole multi-org
# design rests on — so the tests re-prove it rather than trusting this comment.
set -euo pipefail

BASE="${GRAFANA_URL:-http://localhost:3900}"
ADMIN_USER="${GRAFANA_ADMIN_USER:-admin}"
ADMIN_PASS="${GRAFANA_ADMIN_PASSWORD:-admin}"
ORG2_NAME="${GRAFANA_ORG2_NAME:-Sales}"
EXPORT=0
[ "${1:-}" = "--export" ] && EXPORT=1

log() { [ "$EXPORT" = 1 ] && echo "# $*" >&2 || echo "$*" >&2; }

adm() {
  # $1 = org id, rest = curl args. Note: NOT `-u` plus a separate org switch --
  # the header is stateless, so parallel seeding of two orgs cannot race on
  # whatever "current org" the admin user happens to be pointed at.
  local org="$1"; shift
  curl -sS -u "${ADMIN_USER}:${ADMIN_PASS}" \
    -H "Content-Type: application/json" \
    -H "X-Grafana-Org-Id: ${org}" "$@"
}

jqp() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)" 2>/dev/null || true; }

# ---- wait ------------------------------------------------------------
# compose healthchecks already gate this, but a human running the script by hand
# has no such gate -- and a half-booted Grafana 404s in ways that read like bugs.
log "waiting for Grafana at ${BASE} ..."
for i in $(seq 1 60); do
  if curl -sf "${BASE}/api/health" >/dev/null 2>&1; then break; fi
  [ "$i" = 60 ] && { echo "Grafana never became healthy at ${BASE}" >&2; exit 1; }
  sleep 2
done
VERSION=$(curl -sf "${BASE}/api/health" | jqp 'd["version"]')
log "Grafana ${VERSION} is up"

# ---- org 2 -----------------------------------------------------------
# Idempotent: re-running the script must not fail on an existing org, or the
# first thing a contributor does after a flaky test is hit a confusing 409.
ORG2_ID=$(adm 1 "${BASE}/api/orgs" | jqp "next((o['id'] for o in d if o['name']=='${ORG2_NAME}'), '')")
if [ -z "${ORG2_ID}" ]; then
  ORG2_ID=$(adm 1 -X POST "${BASE}/api/orgs" -d "{\"name\":\"${ORG2_NAME}\"}" | jqp 'd["orgId"]')
  log "created org ${ORG2_ID} (${ORG2_NAME})"
else
  log "org ${ORG2_ID} (${ORG2_NAME}) already exists"
fi

# ---- service accounts + tokens --------------------------------------
# A token's value is shown ONCE, at creation, and is never readable again -- so
# a re-run cannot recover the old token and must mint a fresh one. We delete the
# service account and recreate it rather than accumulating tokens.
mint_sa() {         # $1=org  $2=name  $3=role  -> prints the token
  local org="$1" name="$2" role="$3" id token
  id=$(adm "$org" "${BASE}/api/serviceaccounts/search?query=${name}" \
        | jqp "next((s['id'] for s in d.get('serviceAccounts',[]) if s['name']=='${name}'), '')")
  if [ -n "$id" ]; then
    adm "$org" -X DELETE "${BASE}/api/serviceaccounts/${id}" >/dev/null
  fi
  id=$(adm "$org" -X POST "${BASE}/api/serviceaccounts" \
        -d "{\"name\":\"${name}\",\"role\":\"${role}\",\"isDisabled\":false}" | jqp 'd["id"]')
  [ -n "$id" ] || { echo "could not create service account ${name} in org ${org}" >&2; exit 1; }

  # The token name is derived from the account name, and that is NOT cosmetic:
  # token names must be unique **per ORGANISATION**, not per service account.
  # Reusing one name across three accounts in org 1 gets
  #   400 serviceaccounts.ErrTokenAlreadyExists
  #     "service account token with given name already exists in the organization"
  # for the second and third -- which is how the first version of this script
  # silently handed back two empty tokens and every role test "passed" by
  # skipping. Measured, not guessed.
  token=$(adm "$org" -X POST "${BASE}/api/serviceaccounts/${id}/tokens" \
            -d "{\"name\":\"ci-${name}\"}" | jqp 'd["key"]')
  # Fail loudly. An empty token here does not break anything until a test tries
  # to use it, and by then it looks like a permissions problem rather than a
  # bootstrap problem -- the exact confusion this guard removes.
  [ -n "$token" ] || { echo "could not mint a token for ${name} in org ${org}" >&2; exit 1; }
  echo "$token"
}

# Org 1 gets one of each role. The role matrix is a TEST (tests/test_roles_live.py),
# not a comment, because the claim "you need Admin" was written as a comment once
# and was wrong for weeks.
TOK_ADMIN=$(mint_sa 1 grafana-cli-admin Admin)
TOK_EDITOR=$(mint_sa 1 grafana-cli-editor Editor)
TOK_VIEWER=$(mint_sa 1 grafana-cli-viewer Viewer)
# Org 2 gets one, so cross-org refusal can be proven in BOTH directions -- a
# one-sided check would not distinguish "tokens are org-scoped" from "org 2 is
# just broken".
TOK_ORG2=$(mint_sa "${ORG2_ID}" grafana-cli-org2 Admin)
log "minted service-account tokens: admin/editor/viewer in org 1, admin in org ${ORG2_ID}"

# ---- datasources -----------------------------------------------------
# Created through the real HTTP API, not compose provisioning files, on purpose:
# provisioning seeds by a path the CLI never touches, so it would drift from how
# a datasource actually gets made and hide shape changes.
mk_ds() {           # $1=org $2=name $3=type $4=url $5=isDefault
  local existing
  existing=$(adm "$1" "${BASE}/api/datasources" | jqp "next((x['uid'] for x in d if x['name']=='$2'), '')")
  if [ -n "$existing" ]; then echo "$existing"; return; fi
  adm "$1" -X POST "${BASE}/api/datasources" \
    -d "{\"name\":\"$2\",\"type\":\"$3\",\"access\":\"proxy\",\"url\":\"$4\",\"isDefault\":$5}" \
    | jqp 'd["datasource"]["uid"]'
}

# Service names, not localhost: Grafana reaches these over the compose network.
LOKI_UID=$(mk_ds 1 Loki loki http://loki:3100 true)
PROM_UID=$(mk_ds 1 Mimir prometheus http://prometheus:9090 false)
# Org 2 gets its OWN Loki pointing at the SAME server. That is the point: the uid
# differs per org even when the backend is identical, which is exactly the trap
# the per-profile sticky context exists to prevent.
LOKI2_UID=$(mk_ds "${ORG2_ID}" Loki loki http://loki:3100 true)
# A datasource whose backend does not exist, so `DatasourceUnreachable` (exit 8)
# is covered by a real 502 rather than a mock. The production instance had one of
# these by accident (a proxy that was down); here we make it on purpose.
DEAD_UID=$(mk_ds 1 "Loki (dead)" loki http://127.0.0.1:6666 false)
log "datasources: loki=${LOKI_UID} prom=${PROM_UID} org2-loki=${LOKI2_UID} dead=${DEAD_UID}"

# ---- seed logs -------------------------------------------------------
# Pushed straight into Loki (auth_enabled: false -> no tenant header needed).
# Timestamps are NANOSECONDS, 19 digits: Loki reads <=10 digits as seconds and
# anything longer as nanos, so millis would land in 1970 and this would seed
# nothing while reporting success. That trap is the reason this comment exists.
seed_logs() {
  local now_ns lines
  now_ns=$(python3 -c 'import time; print(int(time.time()*1_000_000)*1000)')
  # Deliberate label cardinality, so `logs sources` has something to assert:
  #   hostname     -> 3 values  (useful)
  #   service_name -> 2 values  (useful)
  #   job          -> 1 value   (USELESS as a filter -- the whole reason
  #                              `logs sources` reports counts, not names)
  lines=$(python3 - "$now_ns" <<'PY'
import json, sys
now = int(sys.argv[1])
streams = []
def s(host, svc, msgs):
    streams.append({
        "stream": {"hostname": host, "service_name": svc, "job": "seed"},
        "values": [[str(now - i * 1_000_000_000), m] for i, m in enumerate(msgs)],
    })
# A repeated problem with varying ids -- the fingerprinter must collapse these
# to ONE finding. The ids are non-hex on purpose: Docker Swarm ids broke this
# for real, and a hex-only seed would not have caught it.
s("web1", "api", [
    'level=error msg="fatal task error" task.id=54syhoorqyabcdefghijklmno error="No such image: reg/app:c1f0382b0f"',
    'level=error msg="fatal task error" task.id=91zzhoorqyabcdefghijklmno error="No such image: reg/app:9ab3c7d1e2"',
    'level=error msg="fatal task error" task.id=22aahoorqyabcdefghijklmno error="No such image: reg/app:77de1f0a99"',
    'level=info msg="starting up" version=1.2.3',
])
s("web2", "api", [
    'level=warn msg="option --legacy is deprecated and will be removed in 2.0"',
    'level=error msg="connection to 10.0.0.7:5432 failed after 1.2s"',
    'level=error msg="connection to 10.0.0.9:5432 failed after 0.4s"',
])
s("web3", "worker", [
    'panic: runtime error: invalid memory address or nil pointer dereference',
    'level=info msg="all good"',
])
print(json.dumps({"streams": streams}))
PY
)
  curl -sS -X POST "http://localhost:3100/loki/api/v1/push" \
    -H "Content-Type: application/json" --data-binary "$lines" -o /dev/null -w '%{http_code}'
}
CODE=$(seed_logs)
[ "$CODE" = "204" ] || { echo "Loki push failed with HTTP ${CODE}" >&2; exit 1; }
log "seeded logs into Loki (3 hosts, 2 services, 1 job)"

# Loki accepts a push before it is queryable; poll until the labels appear rather
# than sleeping a guessed amount. An empty query result is a SUCCESS in Loki, so
# a race here would not fail loudly -- it would silently make every log test
# assert against nothing.
for i in $(seq 1 30); do
  N=$(curl -sf "http://localhost:3100/loki/api/v1/labels" | jqp 'len(d.get("data") or [])')
  [ "${N:-0}" -ge 3 ] && break
  [ "$i" = 30 ] && { echo "Loki never made the seeded labels queryable" >&2; exit 1; }
  sleep 1
done
log "Loki is serving ${N} labels"

# ---- alerting --------------------------------------------------------
FOLDER_UID=$(adm 1 "${BASE}/api/folders" | jqp "next((f['uid'] for f in d if f['title']=='Test'), '')")
if [ -z "${FOLDER_UID}" ]; then
  FOLDER_UID=$(adm 1 -X POST "${BASE}/api/folders" -d '{"title":"Test"}' | jqp 'd["uid"]')
fi

# THE headline scenario, reproduced deliberately: a contact point with NO
# integrations, wired into the default notification policy. An alert routed here
# fires forever and reaches nobody, and every Grafana screen shows it working.
# This is the exact state the production instance was in, and `alert route` /
# `notify check` exist to name it -- so the test stack must be able to reproduce
# it, or the feature's headline test would have nothing to bite on.
#
# A contact point that CAN deliver, so the tests can tell the two apart -- a
# suite where everything is broken proves only that the tool says "broken".
# We do NOT rewire the notification policy: a fresh org already routes everything
# to its default `empty` receiver (zero integrations = the black hole), which is
# both the headline scenario and the honest out-of-the-box state. An earlier
# version PUT a policy pointing at a `blackhole` receiver that was never created;
# Grafana rejected it (400 "receiver 'blackhole' does not exist") and the `||`
# swallowed the error, so the intended routing silently never applied and the
# default did the work by accident. Don't reach for a receiver you didn't make.
adm 1 -X POST "${BASE}/api/v1/provisioning/contact-points" -d '{
  "name": "reachable", "type": "webhook", "settings": {"url": "http://localhost:1/hook"}
}' >/dev/null 2>&1 || true

# An alert rule that ALWAYS fires, so `alert firing` and the end-to-end routing
# test have real data and do not skip -- without it the headline feature ("this
# alert reaches nobody") would ship with no live coverage.
#
# The SHAPE is load-bearing and was got wrong first. A single `math: 1 > 0` as
# the condition, with `for: 10s`, was NEVER evaluated by the scheduler
# (lastEvaluation stayed at the zero time) -- Grafana wants a proper reduce/
# threshold condition, not a bare boolean math. The working shape, measured:
#   A = math `1`   ->   C = threshold `A > 0`   (condition = C)
# plus `for: 0s` so it fires on the first evaluation instead of pending, and
# `noDataState/execErrState: Alerting` so it fires even if a future Grafana
# decides the expression yields nothing. This reliably reaches Firing in ~10s.
if ! adm 1 "${BASE}/api/v1/provisioning/alert-rules" | grep -q '"title":"Always Firing"'; then
  adm 1 -X POST "${BASE}/api/v1/provisioning/alert-rules" -d "{
    \"title\": \"Always Firing\",
    \"folderUID\": \"${FOLDER_UID}\",
    \"ruleGroup\": \"seed\",
    \"orgID\": 1,
    \"for\": \"0s\",
    \"condition\": \"C\",
    \"noDataState\": \"Alerting\",
    \"execErrState\": \"Alerting\",
    \"labels\": {\"seeded\": \"true\"},
    \"annotations\": {\"summary\": \"fires forever, notifies nobody -- on purpose\"},
    \"data\": [
      {\"refId\": \"A\", \"datasourceUid\": \"__expr__\", \"relativeTimeRange\": {\"from\": 600, \"to\": 0},
       \"model\": {\"refId\": \"A\", \"type\": \"math\", \"expression\": \"1\"}},
      {\"refId\": \"C\", \"datasourceUid\": \"__expr__\", \"relativeTimeRange\": {\"from\": 600, \"to\": 0},
       \"model\": {\"refId\": \"C\", \"type\": \"threshold\", \"expression\": \"A\",
                   \"conditions\": [{\"evaluator\": {\"type\": \"gt\", \"params\": [0]}}]}}
    ]
  }" >/dev/null || log "note: could not seed the always-firing rule (continuing)"
fi

# Evaluate every 10s instead of the default minute, so the rule reaches Firing
# inside a CI run rather than after it. 10s is unifiedAlerting's minInterval.
adm 1 -X PUT "${BASE}/api/v1/provisioning/folder/${FOLDER_UID}/rule-groups/seed" \
  -d "{\"title\":\"seed\",\"folderUid\":\"${FOLDER_UID}\",\"interval\":10}" >/dev/null 2>&1 || true

# Wait for it to actually fire, so the tests do not race the scheduler. Polling,
# not sleeping: a fixed sleep is either too short (flaky) or too long (wasted on
# every run), and here we can just ask.
log "waiting for the seeded rule to reach Firing ..."
FIRING=0
for i in $(seq 1 18); do
  FIRING=$(adm 1 "${BASE}/api/alertmanager/grafana/api/v2/alerts" | jqp 'len(d)')
  [ "${FIRING:-0}" -gt 0 ] && break
  sleep 5
done
if [ "${FIRING:-0}" -gt 0 ]; then
  log "${FIRING} alert(s) firing — routed to the default 'empty' receiver, which notifies nobody"
else
  # Not fatal: the tests skip rather than fail, and a slow scheduler must not
  # break someone's local run. But say so, or the skip looks like a code problem.
  log "note: nothing firing yet; the firing tests will skip"
fi

# NOTE for anyone reading the seeded state and expecting a hand-made black hole:
# a fresh Grafana org already ships one. Its default contact point is named
# `empty` and has ZERO integrations, and the default policy routes everything to
# it -- so out of the box, every alert Grafana raises notifies nobody. That is
# not a quirk of this test stack; it is exactly what the production instance this
# tool was built against looked like. `grafana-cli alert route` exists to say it out loud.
log "seeded alerting: folder=${FOLDER_UID}, a reachable contact point, an always-firing rule"

# ---- output ----------------------------------------------------------
emit() {
  local prefix="$1"
  cat <<EOF
${prefix}GRAFANA_URL=${BASE}
${prefix}GRAFANA_TOKEN=${TOK_ADMIN}
${prefix}GRAFANA_TOKEN_EDITOR=${TOK_EDITOR}
${prefix}GRAFANA_TOKEN_VIEWER=${TOK_VIEWER}
${prefix}GRAFANA_TOKEN_ORG2=${TOK_ORG2}
${prefix}GRAFANA_ORG2_ID=${ORG2_ID}
${prefix}GRAFANA_TEST_FOLDER=${FOLDER_UID}
${prefix}GRAFANA_TEST_LOKI_UID=${LOKI_UID}
${prefix}GRAFANA_TEST_PROM_UID=${PROM_UID}
${prefix}GRAFANA_TEST_DEAD_UID=${DEAD_UID}
${prefix}GRAFANA_ALLOW_WRITES=1
EOF
}

if [ "$EXPORT" = 1 ]; then
  emit "export "
else
  # GITHUB_ENV when present, so the CI job needs no plumbing of its own.
  if [ -n "${GITHUB_ENV:-}" ]; then
    emit "" >> "${GITHUB_ENV}"
    log "wrote the test environment to \$GITHUB_ENV"
  fi
  emit ""
fi

# GRAFANA_ALLOW_WRITES is the safety interlock and it is deliberately NOT a
# default. The destructive tests refuse to run without it, so pointing the suite
# at a real Grafana can read and can never write -- and only this script, which
# by construction only ever talks to a throwaway stack, turns it on.
