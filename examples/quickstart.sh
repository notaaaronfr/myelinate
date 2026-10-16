#!/usr/bin/env bash
# Myelinate REST walkthrough: import a skill, recall, record a verified run, send feedback, read insights.
#
#   MYELINATE_URL=http://localhost:8080 ./examples/quickstart.sh
#
# Needs curl and jq. Against a dev gateway (MYELINATE_ENV=dev) no API key is needed. Otherwise set
# MYELINATE_API_KEY to a key with the `learn` and `review` scopes (importing a skill needs `review`).
set -euo pipefail

URL="${MYELINATE_URL:-http://localhost:8080}"
AGENT="${AGENT_ID:-quickstart-bot}"
AUTH=()
if [[ -n "${MYELINATE_API_KEY:-}" ]]; then AUTH=(-H "Authorization: Bearer ${MYELINATE_API_KEY}"); fi

api() {  # api METHOD PATH [JSON_BODY]
  local method=$1 path=$2
  if [[ $# -ge 3 ]]; then
    curl -fsS -X "$method" "$URL$path" ${AUTH[@]+"${AUTH[@]}"} -H 'content-type: application/json' -d "$3"
  else
    curl -fsS -X "$method" "$URL$path" ${AUTH[@]+"${AUTH[@]}"}
  fi
}

echo "== 0. health"
api GET /healthz; echo

echo "== 1. import an existing SKILL.md (needs the 'review' scope)"
SKILL_MD=$(cat <<'MD'
---
name: rotate-tls-cert
description: Rotate an expiring TLS certificate on a Kubernetes ingress
---
## Steps
1. Issue a new certificate with cert-manager by bumping the Certificate resource.
2. Roll the ingress controller pods so they load the new certificate.

## Verification
- Confirm the served certificate expiry with openssl s_client before closing the ticket.
MD
)
api POST /v1/skills/import "$(jq -n --arg a "$AGENT" --arg md "$SKILL_MD" '{agent_id: $a, markdown: $md}')" | jq .

echo "== 2. recall before the run"
RECALL=$(api POST /v1/recall "$(jq -n --arg a "$AGENT" '{
  scope: {agent_id: $a, user_id: "user-42"},
  query: "the TLS certificate on api.example.com expires tomorrow, rotate it",
  session_id: "sess-1"}')")
echo "$RECALL" | jq '{recall_id, holdout, skills: [.skills[].name]}'
echo "--- instructions to prepend to the system prompt:"
echo "$RECALL" | jq -r .instructions
RECALL_ID=$(echo "$RECALL" | jq -r .recall_id)

echo "== 3. record the run, with a verification result (the strongest learning signal)"
EPISODE=$(api POST /v1/episodes "$(jq -n --arg a "$AGENT" --arg r "$RECALL_ID" '{
  scope: {agent_id: $a, user_id: "user-42"},
  idempotency_key: ("quickstart:" + $r),
  session_id: "sess-1",
  recall_id: $r,
  task: "the TLS certificate on api.example.com expires tomorrow, rotate it",
  messages: [
    {role: "user", content: "the TLS certificate on api.example.com expires tomorrow, rotate it"},
    {role: "assistant", content: "Bumped the Certificate resource and restarted the ingress pods."}],
  tool_calls: [{name: "kubectl", arguments: {cmd: "rollout restart deploy/ingress-nginx"}, result: "restarted"}],
  outcome: "success",
  verification: {passed: true, details: "openssl s_client: notAfter is 90 days out", source: "ci-check"},
  framework: "curl"}')")
echo "$EPISODE" | jq .
EPISODE_ID=$(echo "$EPISODE" | jq -r .episode_id)

echo "== 4. user feedback on that episode (0.0 - 1.0, optional correction)"
api POST /v1/feedback "$(jq -n --arg e "$EPISODE_ID" '{
  episode_id: $e, score: 0.9, comment: "worked",
  correction: "Also post the new expiry date in the change ticket."}')"
echo "feedback recorded"

echo "== 5. insights for the agent"
api GET "/v1/agents/$AGENT/insights" | jq '{agent_id, learning_lift, skills, pending_changesets}'
