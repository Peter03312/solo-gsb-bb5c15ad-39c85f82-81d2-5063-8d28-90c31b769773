#!/usr/bin/env bash
# Candidate-set pruning walkthrough for the Temporary Event Radio Spectrum API.
#
# This round keeps every HTTP contract and the exact interference/window
# rules unchanged, but each adjudication first narrows the database
# candidate set by time window, coverage bbox and frequency band (the
# guard band only widens the license frequency envelope, never a
# restriction window) and then runs the exact predicates as before.  This
# script demonstrates that records disjoint in those three dimensions no
# longer participate in a preview/decision, while genuinely conflicting
# records are still reported in the same order and with the same codes.
#
# Usage: ./examples/pruning_walkthrough.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
J() { python3 -m json.tool; }
post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "${2:-}"; }

echo "== health =="
curl -s "$BASE/health"; echo

echo "== 1. one effective license in the candidate's vicinity (2026-11-15 morning) =="
NEAR=$(post /api/applications '{
  "name":"Nearby stage",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2026-11-15T08:00:00Z","end":"2026-11-15T12:00:00Z"}
}')
NEAR_ID=$(echo "$NEAR" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$NEAR_ID/approve" '{"expected_revision":0}' | J

echo "== 2. many effective licenses that are irrelevant: disjoint time, =="
echo "    coverage and frequency (they stay effective, they just never    =="
echo "    reach the adjudication candidate set)                           =="
FIRST_FAR=""
for d in 02 03 04 05; do
  for h in 00 06 12 18; do
    FAR=$(post /api/applications "{
      \"name\":\"far-$d-$h\",
      \"circle\":{\"cx\":90000,\"cy\":90000,\"radius\":1},
      \"frequency\":{\"low\":5000,\"high\":5010},
      \"time_window\":{\"start\":\"2026-11-$d"T$h:00:00Z\",\"end\":\"2026-11-$d"T$h:30:00Z\"}
    }")
    ID=$(echo "$FAR" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
    REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
    post "/api/applications/$ID/approve" "{\"expected_revision\":$REV}" >/dev/null
    [ -z "$FIRST_FAR" ] && FIRST_FAR="$ID"
  done
done
echo "approved $(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["effective_application_ids"]))') effective licenses total"

echo "== 3. candidate overlapping the nearby license -> only that one conflict =="
echo "    (same ordering/detail shape as before; the far licenses are absent)"
CAND=$(post /api/applications '{
  "name":"New morning event",
  "circle":{"cx":100,"cy":0,"radius":1000},
  "frequency":{"low":105,"high":108},
  "time_window":{"start":"2026-11-15T09:00:00Z","end":"2026-11-15T11:00:00Z"}
}')
CAND_ID=$(echo "$CAND" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$CAND_ID/preview" | J

echo "== 4. approving it is refused (409 interference); far licenses do not matter =="
REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post "/api/applications/$CAND_ID/approve" "{\"expected_revision\":$REV}" | J || true

echo "== 5. a disjoint-time candidate previews clean even with the band busy =="
LATER=$(post /api/applications '{
  "name":"Afternoon event",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2026-11-15T13:00:00Z","end":"2026-11-15T17:00:00Z"}
}')
LATER_ID=$(echo "$LATER" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$LATER_ID/preview" | J

echo "== 6. available-ranges: only nearby-in-time/area grants shape the answer =="
post "/api/applications/$LATER_ID/available-ranges" '{
  "candidate_low_min":80,"candidate_low_max":140
}' | J

echo "== 7. restriction windows are pruned WITHOUT the guard band: activate a =="
echo "    window far away in time/area/band, then a window actually hitting  =="
echo "    the afternoon candidate; only the latter shows up as a hit          =="
WREV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post /api/restriction-windows "{
  \"expected_revision\":$WREV,
  \"reason\":\"distant exercise\",
  \"circle\":{\"cx\":-90000,\"cy\":-90000,\"radius\":1},
  \"frequency\":{\"low\":7000,\"high\":7010},
  \"time_window\":{\"start\":\"2026-12-01T00:00:00Z\",\"end\":\"2026-12-01T02:00:00Z\"}
}" >/dev/null
WREV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post /api/restriction-windows "{
  \"expected_revision\":$WREV,
  \"reason\":\"local control\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":104,\"high\":106},
  \"time_window\":{\"start\":\"2026-11-15T14:00:00Z\",\"end\":\"2026-11-15T15:00:00Z\"}
}" | J
echo "-- preview of the afternoon candidate: exactly one window hit --"
post "/api/applications/$LATER_ID/preview" | J

echo "== 8. guard-band change still checks ALL current effective pairs =="
echo "    (every effective license is loaded, pruned or not; only this path is unpruned)"
post /api/config/guard-band/preview '{"guard_band":2.0}' | python3 -c '
import json,sys
b=json.load(sys.stdin)
print("effective_application_ids:", b["effective_application_ids"])
print("interfering_pair_count:", b["interfering_pair_count"])'

echo "== 9. batch approval of disjoint-time members stays atomic =="
B1=$(post /api/applications '{"name":"batch-a","circle":{"cx":0,"cy":0,"radius":50},"frequency":{"low":800,"high":801},"time_window":{"start":"2027-01-01T00:00:00Z","end":"2027-01-01T01:00:00Z"}}')
B2=$(post /api/applications '{"name":"batch-b","circle":{"cx":0,"cy":0,"radius":50},"frequency":{"low":900,"high":901},"time_window":{"start":"2027-01-02T00:00:00Z","end":"2027-01-02T01:00:00Z"}}')
B1_ID=$(echo "$B1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
B2_ID=$(echo "$B2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post /api/applications/batch-approve \
  "{\"expected_revision\":$REV,\"application_ids\":[$B1_ID,$B2_ID]}" | J

echo "== done: previews/writes stayed exact; only the candidate sets shrank =="
