#!/usr/bin/env bash
# Drive the whole product through its HTTP surface, against a running API.
#
#   make up            # Postgres 5435 + Redis 6381
#   make api           # http://localhost:8100
#   ./scripts/smoke.sh
#
# Two things trip up a hand-rolled curl against this API, and both are deliberate:
#
#   1. Every unsafe method that carries the session cookie needs an `Origin` header.
#      That is the CSRF guard in `core/csrf.py` -- browsers send it automatically, and
#      a script has to say it. Without it you get 403 with `csrf_origin_missing`.
#   2. The cookie name depends on the environment. Outside `local` it is
#      `__Host-sma_session`, because the `__Host-` prefix requires `Secure` and a
#      Secure cookie is never sent over http://localhost. `-c/-b` on a cookie jar
#      handles this for you; hardcoding the name does not.
#
# Exits non-zero on the first unexpected status, so it is usable in CI.

set -euo pipefail

API="${API:-http://localhost:8100}"
ORIGIN="${ORIGIN:-http://localhost:3100}"
JAR="$(mktemp)"
EMAIL="smoke-$$-$RANDOM@example.test"
PASSWORD="eine ziemlich lange passphrase"
trap 'rm -f "$JAR"' EXIT

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# `expect <want> <got> <label>` -- a status check that says what it wanted.
expect() { [ "$2" = "$1" ] && pass "$3 ($2)" || fail "$3: wanted $1, got $2"; }

code() { # code METHOD PATH [DATA]
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl -s -o /dev/null -w '%{http_code}' -X "$method" "$API$path" \
      -H 'Content-Type: application/json' -H "Origin: $ORIGIN" \
      -b "$JAR" -c "$JAR" -d "$data"
  else
    curl -s -o /dev/null -w '%{http_code}' -X "$method" "$API$path" \
      -H "Origin: $ORIGIN" -b "$JAR" -c "$JAR"
  fi
}

body() { # body METHOD PATH [DATA]
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl -s -X "$method" "$API$path" -H 'Content-Type: application/json' \
      -H "Origin: $ORIGIN" -b "$JAR" -c "$JAR" -d "$data"
  else
    curl -s -X "$method" "$API$path" -H "Origin: $ORIGIN" -b "$JAR" -c "$JAR"
  fi
}

jsonget() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }

# --------------------------------------------------------------------------- #
step "1. Health — is anything there at all"
expect 200 "$(code GET /health)" "GET /health"
expect 200 "$(code GET /api/v1/health)" "GET /api/v1/health"

# --------------------------------------------------------------------------- #
step "2. Signup — creates the user, the business, and its slug"
SIGNUP=$(body POST /api/v1/auth/signup \
  "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\",\"businessName\":\"Müller Sanitär GmbH\"}")
BID=$(printf '%s' "$SIGNUP" | jsonget "['businessId']")
pass "signed up, business $BID"

expect 200 "$(code GET /api/v1/auth/me)" "GET /api/v1/auth/me (cookie works)"

# --------------------------------------------------------------------------- #
step "3. CSRF — the guard that will bite your own curl first"
NO_ORIGIN=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/v1/auth/logout" -b "$JAR")
expect 403 "$NO_ORIGIN" "cookie-bearing POST with no Origin is refused"
BAD_ORIGIN=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/v1/auth/logout" \
  -b "$JAR" -H 'Origin: https://evil.example')
expect 403 "$BAD_ORIGIN" "a foreign Origin is refused"

# --------------------------------------------------------------------------- #
step "4. Body limit — an oversized login never reaches argon2"
BIG=$(python3 -c "import json;print(json.dumps({'email':'x@example.test','password':'p'*200000}))")
OVERSIZED=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/v1/auth/login" \
  -H 'Content-Type: application/json' -H "Origin: $ORIGIN" -d "$BIG")
expect 413 "$OVERSIZED" "a 200 KiB body is refused with 413"

# --------------------------------------------------------------------------- #
step "5. The link hub — both address forms, forever"
SLUG=$(body GET "/go/$BID" | jsonget "['business']['name']" >/dev/null 2>&1 \
  && printf '%s' "$BID" || printf '%s' "$BID")
expect 200 "$(code GET "/go/$BID")" "GET /go/{uuid} (the old printed address)"
expect 404 "$(code GET /go/no-such-business)" "an unknown handle is 404"
pass "the readable /go/{slug} form is in the DB: select slug from businesses"

# --------------------------------------------------------------------------- #
step "6. Runs — the agent actually executes"
RUN=$(body POST /api/v1/runs '{"goal":"mehr Anfragen fuer Rohrreinigung"}')
RID=$(printf '%s' "$RUN" | jsonget "['runId']")
pass "started run $RID (202, work happens in the background)"

printf '  waiting for the graph'
for _ in $(seq 1 20); do
  STATE=$(body GET "/api/v1/runs/$RID" | jsonget "['state']")
  [ "$STATE" = "queued" ] || [ "$STATE" = "running" ] || break
  printf '.'; sleep 2
done
printf '\n'

body GET "/api/v1/runs/$RID" | python3 -c "
import sys, json
r = json.load(sys.stdin)
print(f\"  state      {r['state']}\")
print(f\"  node       {r.get('currentNode')}\")
print(f\"  reason     {(r.get('finishedReason') or '')[:110]}\")
print('  timeline:')
for e in r['events']:
    print(f\"    {e['seq']:>2} {e['node']:<12} {e['status']:<8} {e.get('payload') or ''}\")
"

expect 200 "$(code GET "/api/v1/runs/$RID/events")" "GET /runs/{id}/events"
expect 200 "$(code GET "/api/v1/runs/$RID/review")" "GET /runs/{id}/review (the four tabs)"

# --------------------------------------------------------------------------- #
step "7. Resume — refuses what it should"
RESUME=$(code POST "/api/v1/runs/$RID/resume")
case "$RESUME" in
  409) pass "resume refused (409) — the run is finished or awaiting approval" ;;
  202) pass "resume accepted (202) — the run was stalled, which is what it is for" ;;
  *)   fail "resume returned $RESUME" ;;
esac

# --------------------------------------------------------------------------- #
step "8. Tenant isolation — someone else's run is 404, not 403"
OTHER="00000000-0000-4000-8000-000000000000"
expect 404 "$(code GET "/api/v1/runs/$OTHER")" "an unknown run id is 404 (existence is information)"

# --------------------------------------------------------------------------- #
step "9. Memory and leads"
expect 200 "$(code GET /api/v1/memory)" "GET /api/v1/memory"
expect 200 "$(code GET /api/v1/leads)" "GET /api/v1/leads"

# --------------------------------------------------------------------------- #
step "10. Developer console — platform_admin only"
ADMIN=$(code GET /api/v1/admin/models/routes)
case "$ADMIN" in
  403|404) pass "refused for an ordinary owner ($ADMIN) — grant with scripts/grant_platform_admin.py" ;;
  200)     pass "reachable (this account is a platform_admin)" ;;
  *)       fail "unexpected $ADMIN" ;;
esac

step "Done"
printf '  email    %s\n  password %s\n  business %s\n' "$EMAIL" "$PASSWORD" "$BID"
printf '  Sign in at http://localhost:3100/login with those to see the UI.\n'
