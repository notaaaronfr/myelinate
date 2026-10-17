# Human-approved learning (propose-only)

Some teams already have a process for changing what agents do. Instructions and skills live in a content store, a
person promotes each version, and every run locks the version it started with. Myelinate can fit that process
instead of replacing it. In **propose-only** mode it never changes anything itself: it watches outcomes, works out
what should change, and hands each suggestion to your system for a person to promote.

This page describes the setup and a low-risk way to trial it on one workflow.

## What propose-only changes

With `MYELINATE_GOVERNANCE_MODE=propose_only`:

- **Nothing is applied automatically.** Every skill change becomes a pending changeset, whatever its risk level, and
  every learned memory is held as `pending`.
- **No canaries.** Live traffic never sees an unapproved version.
- **Recall serves only approved knowledge.** Pending suggestions are never injected into an agent.
- **Suggestions are exported.** Each one is available as a reviewable proposal, and can be pushed to your system by
  a signed webhook.
- **Approval is explicit.** A change goes live in Myelinate only when someone approves it (`POST
  /v1/changesets/{id}/review`). If your content store is the source of truth, promote the suggestion there, then
  approve it here or re-import the promoted SKILL.md (`POST /v1/skills/import`), so both stay in step.

## Run it as a separate service

Install the client in your agent codebase and run the gateway on its own:

```bash
# in the agent's codebase: just the client (httpx + pydantic), with the MAF adapter for MAF 1.15+
pip install "myelinate[maf]"

# the gateway, as its own service (container or VM)
docker run -d --name myelinate -p 8080:8080 --env-file myelinate.env ghcr.io/notaaaronfr/myelinate:0.4.0
```

The client never pulls in the gateway's web framework, database or MCP libraries, so your pinned versions don't
move. CI checks this against a pinned stack on every change.

## Configuration for a trial

```bash
# myelinate.env
MYELINATE_BOOTSTRAP_ADMIN_KEY=<long random value>
MYELINATE_DATABASE_URL=postgresql+asyncpg://myelinate:<password>@db:5432/myelinate

# Never change anything by itself; everything is a suggestion
MYELINATE_GOVERNANCE_MODE=propose_only

# Don't store conversation text: keep the task, outcome, verification details and tool errors only
MYELINATE_CAPTURE_MODE=outcomes
MYELINATE_REDACT_PII=true
MYELINATE_EPISODE_RETENTION_DAYS=30

# Push each suggestion to your review or content system
MYELINATE_WEBHOOK_URL=https://content-store.internal/hooks/myelinate
MYELINATE_WEBHOOK_SECRET=<shared secret>

# Learning model through OpenRouter, with prompt logging and training on your data refused
MYELINATE_LLM_PROVIDER=openai_compatible
MYELINATE_LLM_BASE_URL=https://openrouter.ai/api/v1
MYELINATE_LLM_API_KEY=<openrouter key>
MYELINATE_LLM_MODEL=anthropic/claude-opus-5.5        # the provider's model id; check OpenRouter's model list
MYELINATE_LLM_EXTRA_BODY={"provider": {"data_collection": "deny"}}
```

The learning model is only Myelinate's. Your agents keep using whatever model they use today.

`MYELINATE_LLM_EXTRA_BODY` is merged into every learning request, so provider routing and privacy settings stay in
configuration. The same provider setting works for Azure OpenAI, vLLM, LiteLLM and Ollama. For models without
structured outputs, set `MYELINATE_LLM_JSON_MODE=json_object`.

## Watch-only: record, don't recall

For the safest trial, don't change the agent's prompt at all: skip `recall` and only `record` outcomes. Myelinate
learns from what happens, and its suggestions arrive by webhook, but the agent behaves exactly as before.

```python
from myelinate import GatewayClient
from myelinate.models import EpisodeIn, Outcome, Scope, Verification

client = GatewayClient("https://myelinate.internal", api_key="myl_...")

# after each run of the workflow you're trialling
await client.record(EpisodeIn(
    scope=Scope(agent_id="research"),
    task="Screen supplier shortlist for programme X",
    outcome=Outcome.failure,
    verification=Verification(passed=False, source="screening-check",
                              details="all 14 suppliers rejected with the same reason"),
))
```

Good outcome signals are the checks you already run: grounding results, screening reasons, a reviewer's change of a
record's status, a person's correction. The more specific the `details`, the better the suggestions.

## The suggestion

Each suggestion is a changeset. `GET /v1/changesets/{id}/proposal` (and the `changeset.proposed` webhook) returns,
for every skill it touches:

- `current_skill_md` and `proposed_skill_md`: the whole skill before and after, in SKILL.md format
- `diff`: a unified diff between them
- `ops`: the individual rule edits
- `rationale`, `risk`, `safety_findings` and `evidence`, which lists the episodes it came from with their outcomes
  and verification details

```json
{
  "changeset_id": "cs_8f2…",
  "status": "pending",
  "risk": "medium",
  "rationale": "Every supplier was rejected for the same criterion; the screening rule applied the threshold to the wrong field.",
  "skills": [{
    "skill": "supplier-screening",
    "current_version": 3,
    "diff": "--- supplier-screening/SKILL.md (v3)\n+++ supplier-screening/SKILL.md (proposed)\n@@ …",
    "proposed_skill_md": "---\nname: supplier-screening\n…"
  }],
  "evidence": [{"episode_id": "ep_…", "outcome": "failure", "verification": {"passed": false, "details": "…"}}]
}
```

## Webhook deliveries

Myelinate POSTs JSON to `MYELINATE_WEBHOOK_URL` for the events in `MYELINATE_WEBHOOK_EVENTS`:

| Event | When | `data` |
|---|---|---|
| `changeset.proposed` | A suggestion is waiting for review | The proposal above |
| `changeset.reviewed` | Someone approved or rejected it | The proposal, with the new `status` |
| `memory.pending` | A learned memory is waiting for approval | The memory, with `approve_url` |

Every delivery carries `X-Myelinate-Event`, `X-Myelinate-Delivery` (a unique id; retries reuse it, so deduplicate
on it) and `X-Myelinate-Signature: sha256=<hex HMAC-SHA256 of the raw body with your secret>`. Verify the signature
before trusting the payload:

```python
import hashlib
import hmac

def verified(body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

Deliveries are durable jobs. A non-2xx response is retried with backoff, up to `MYELINATE_JOB_MAX_ATTEMPTS` times.

## Evaluating the trial

After a few weeks, compare Myelinate's suggestions with the fixes your team made by hand over the same period:

- Did it flag the same problems? How long after they first appeared?
- Were the suggested rules ones a reviewer would have written?
- How many suggestions were rejected, and why? Rejections are also fed back, so it stops proposing the same edit.

`GET /v1/changesets?status=` lists everything it proposed, with statuses and reviewers, for that comparison.
