# Governance

Every change to a learned skill goes through a **changeset**: a bounded batch of typed operations that is scanned,
risk-rated, approved by policy or a human, optionally canaried on live traffic, applied atomically as a new immutable
version, and reversible. This page explains the lifecycle and the settings that control it, and shows how to
operate it through the API. For the underlying concepts see [Concepts](../getting-started/concepts.md).

## Changeset lifecycle

```
            ┌─────────── reject (human) ───────────────────────────────┐
            │                                                          ▼
 proposed ─► pending ─► approve ─┬─► applied                         rejected
                                 ├─► canary ──► promoted ─► applied
                                 │          └─► rejected / expired ─► rejected
                                 ├─► approved (queued behind a running canary) ─► re-evaluated later
                                 └─► failed (stale: the skill changed since it was proposed)
```

| Status | Meaning |
|---|---|
| `pending` | Waiting for review. Every changeset starts here; auto-approval happens immediately after proposal. |
| `approved` | Approved, but **queued** because a canary is already running on a skill it touches. It is re-evaluated when that canary ends or the skill is rolled back. |
| `canary` | Being served beside the incumbent version on live traffic (see [Canary experiments](#canary-experiments)). |
| `applied` | Applied. `applied_versions` records the new version of each skill. |
| `rejected` | Rejected by a reviewer, by a canary verdict, by canary expiry, or by a rollback while it was in canary. `rejection_reason` says why. |
| `failed` | Couldn't apply because a skill it touches moved to a newer version since the changeset was proposed (optimistic concurrency, no silent rebase), or because a skill it creates already exists. Learning re-proposes against the new version if the evidence still supports it. |

`superseded` appears in a schema comment but is not produced by the current code.

A changeset records `source` (`reflection`, `evolution`, `curation`, `api` or `import`), `ops`, `rationale`,
`risk`, `safety_findings`, `episode_ids` (provenance), `eval_score`/`baseline_score` (evolution only), `reviewer`,
`review_comment` and `base_versions`.

### Operation types and risk tiers

| Operation | Effect |
|---|---|
| `create_skill` | New skill with a description |
| `add` | New rule (bullet) in a section: steps, pitfalls, heuristics, verification, snippets or notes |
| `update` | Rewrite an existing rule |
| `remove` | Retire a rule (status `removed`; never deleted). With `merged_into`, its evidence counters are added to the kept rule. |
| `set_description` | Change a skill's description. The description drives retrieval, so this changes *when* the skill fires. |

| Risk | When | Default policy |
|---|---|---|
| `low` | Only `create_skill` and `add` | Auto-approve (`MYELINATE_AUTO_APPROVE_LOW_RISK=true`) |
| `medium` | Contains `update`, or a `remove` with `merged_into` (a duplicate merge) | Human (`MYELINATE_AUTO_APPROVE_MEDIUM_RISK=false`) |
| `high` | Contains `set_description`, or a `remove` without `merged_into` | **Always human**; no setting changes this |

Each learning changeset is capped at `MYELINATE_MAX_OPS_PER_CHANGESET` operations (default 6); evolution may use up to
three times that.

### Auto-approval rules

A changeset proposed by learning is auto-approved only if **all** of the following hold
(`Governance.auto_approvable`):

1. It has no safety findings other than `lint:*` ones.
2. If it has an `eval_score` (evolution changesets do), the score is at least
   `MYELINATE_AUTO_APPROVE_MIN_EVAL_SCORE` (default 0.7).
3. Its risk tier is allowed by `MYELINATE_AUTO_APPROVE_LOW_RISK` or `MYELINATE_AUTO_APPROVE_MEDIUM_RISK`. High risk is
   never auto-approved.

Auto-approvals are recorded with reviewer `system:policy`.

Skills authored or imported by humans (`POST /v1/skills`, `POST /v1/skills/import`, which need `review`) are applied
immediately without a canary, whatever their risk tier, **unless** the safety scan flags them, in which case they stay
`pending`.

### Safety findings vs lint findings

| Kind | Examples | Effect |
|---|---|---|
| Safety (`secret:*`, `injection:*`, `size:*`) | `secret:github_token`, `injection:ignore (all \|any )?(previous\|prior\|above) (instructions\|rules)`, `size:17000>16000` | Blocks auto-approval. A human must decide. |
| Lint (`lint:*`) | `lint:too-long:95w` (over `MYELINATE_MAX_BULLET_WORDS`, default 80), `lint:too-short` (under 4 words), `lint:incident-log-shape` (reads like a diary entry, not a reusable rule) | Advisory only. Shown to reviewers; doesn't block auto-approval. |

Memories are scanned too. A memory with findings is stored as `pending`, and so is every memory when
`MYELINATE_AUTO_APPROVE_MEMORIES=false`. So is a memory that resembles an existing one when the LLM needed to reconcile
them is unavailable or over budget. Approve one with `POST /v1/memories/{id}/approve`, and list them with
`GET /v1/memories?status=pending&agent_id=...`.

## Reviewing through the API

Reviewing needs the `review` scope.

```bash
GW=https://learner.internal
AUTH="Authorization: Bearer $MYELINATE_REVIEW_KEY"

# Pending changesets (status defaults to "pending"; status= with an empty value lists all)
curl -s "$GW/v1/changesets?agent_id=support-bot&limit=50" -H "$AUTH" | jq '.[] | {id, risk, source, safety_findings, rationale, ops}'

# Other states
curl -s "$GW/v1/changesets?status=canary" -H "$AUTH"
curl -s "$GW/v1/changesets?status=rejected" -H "$AUTH"

# Approve (a medium/high-risk change to a proven skill goes to canary first)
curl -s -X POST "$GW/v1/changesets/cs_123/review" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"approve": true, "comment": "matches the runbook"}'

# Approve and apply immediately, without a canary
curl -s -X POST "$GW/v1/changesets/cs_123/review" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"approve": true, "skip_canary": true, "comment": "urgent fix"}'

# Reject. The comment becomes rejection_reason and is shown to future curation and evolution.
curl -s -X POST "$GW/v1/changesets/cs_123/review" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"approve": false, "comment": "too specific to one customer"}'
```

The response is `{"status": "<new status>"}`: `applied`, `canary`, `approved` (queued), `failed` or `rejected`.
Only `pending` changesets can be reviewed. Anything else returns `409 changeset is <status>`. **A running canary
can't be cancelled through review.** To stop one, roll the skill back (see [Rollback](#rollback)).

Checklist for reviewers:

- Read every `add`/`update` `content`. This is literally text that will be injected into prompts.
- For `remove` and `set_description`, check what stops firing and when.
- Treat any `secret:` or `injection:` finding as a reason to reject unless it is clearly a false positive.
- Check provenance: `episode_ids` link to the runs that produced the change (`GET /v1/agents/{id}/export` returns
  them with rewards).

## Canary experiments

A canary validates a change on real traffic, because offline judges are imperfect.

**When a canary starts.** An approved changeset goes to canary when all of these hold:
`MYELINATE_CANARY_ENABLED=true`; `skip_canary` is false; the risk is `medium` or `high`; it modifies exactly one
existing skill; and that skill has at least `MYELINATE_CANARY_MIN_SAMPLES` uses. Otherwise it is applied directly. A
new skill has no incumbent to compare against, so it is never canaried.

**Serving.** During recall, each time the skill is selected, the gateway picks an arm:

- With probability `MYELINATE_CANARY_FLOOR` (default 0.1) it serves the candidate, with the same probability the
  incumbent, and otherwise it uses **Thompson sampling** on the two arms' Beta posteriors. The floor guarantees both
  arms keep getting traffic.
- **Assignment is sticky.** Within a session, the arm from the session's previous recall is reused. The first choice is
  seeded by a hash of the experiment ID and the `session_id` (or `user_id`), so a conversation doesn't flip between
  versions. Recalls with neither ID are assigned independently.

**Credit.** Each episode's reward, from 0 to 1 (verification > feedback > reported outcome > confident judge), updates
the arm that served it.

**Verdict.** After each credit, once **both** arms have at least `MYELINATE_CANARY_MIN_SAMPLES` (default 20) units of
evidence:

| Condition | Verdict |
|---|---|
| P(candidate better) ≥ `MYELINATE_CANARY_THRESHOLD` (0.95) | **Promote**: the changeset is applied (if the skill hasn't moved; otherwise `failed`) |
| P(candidate better) ≤ 1 − threshold | **Reject** |
| Candidate evidence ≥ `MYELINATE_CANARY_MAX_SAMPLES` (300) and still undecided | **Reject**: inconclusive, so the incumbent is kept |

**Expiry.** Experiments still running after `MYELINATE_CANARY_MAX_DAYS` (14), or whose skill is archived or
deprecated, are rejected with "canary expired without a verdict; incumbent kept". The expiry check runs as part of
the periodic **curate** job, so it only happens when curation runs for some agent in the tenant (every
`MYELINATE_CURATE_EVERY_N_EPISODES` episodes, or `POST /v1/agents/{id}/curate`).

Canaries need traffic. With few runs per skill they will mostly expire, so keep
`MYELINATE_AUTO_APPROVE_MEDIUM_RISK=false` and rely on review.

Running experiments, with `p_candidate_better` and evidence per arm, appear in `GET /v1/agents/{id}/insights`.

## Holdout and learning lift

`MYELINATE_HOLDOUT_RATE` (default 0.05) puts that share of **sessions** in a control group:

- Holdout recalls inject **no learned skills** and no skill index. Memories and the user profile are still served, so
  the measured lift is the lift from skills.
- The skills that *would* have been served are recorded, so treated and holdout runs are compared like for like.
  Only episodes recorded with a `recall_id` where at least one skill applied (or would have) count.
- Holdout is sticky per `session_id`. The recall response has `"holdout": true` for these runs.
- `0` disables the holdout and therefore the lift measurement.

Read `GET /v1/agents/{agent_id}/insights` (`learn` scope):

```json
{
  "learning_lift": {
    "treated_success": 0.81, "treated_n": 412.0,
    "holdout_success": 0.64, "holdout_n": 22.5,
    "lift": 0.168, "lift_90ci": [0.02, 0.31], "significant": true
  },
  "skills": [{"name": "deploy-checks", "version": 4, "status": "active", "uses": 120,
              "success_rate": 0.79, "success_90ci": [0.72, 0.85]}],
  "experiments": [{"id": "exp_…", "status": "running", "p_candidate_better": 0.71,
                   "incumbent_n": 18.0, "candidate_n": 6.5}],
  "pending_changesets": 2,
  "problem_rules": [{"id": "b-1a2b3c4d", "content": "…", "helpful": 1, "harmful": 5}],
  "tenant_learning_cost_30d": {"calls": 310, "input_tokens": 2100000, "output_tokens": 180000, "by_purpose": {"reflect": 900000}}
}
```

- `lift` is the posterior mean of treated minus holdout success rate. `lift_90ci` is its 90% credible interval, and
  `significant` is true when the interval excludes 0.
- `*_n` is reward evidence, not run counts: partial rewards count fractionally, and runs without any reward signal don't
  count.
- At 5% holdout, `holdout_n` grows slowly. Expect wide intervals for weeks on low-traffic agents. Don't act on a
  non-significant lift.
- `problem_rules` lists active rules tagged harmful more often than helpful. These are candidates for review or
  evolution.

## Versions, rollback and pinning

**Versions.** Every applied changeset and every rollback creates an immutable snapshot of the skill's description and
active rules.

```bash
curl -s "$GW/v1/skills/deploy-checks/versions?agent_id=support-bot" -H "$AUTH"
```

Each entry has `version`, `source` (`reflection`, `api`, `rollback:v3`…), `changeset_id`, `created_by`, `created_at`,
`description` and `bullets`.

### Rollback

```bash
curl -s -X POST "$GW/v1/skills/skl_abc/rollback" -H "$AUTH" -H "Content-Type: application/json" -d '{"version": 2}'
# → {"version": 5}   the restored content becomes a new version; history is never rewritten
```

Rollback (`review` scope) restores the rules and description of the target version. It also:

- Stops any running canary on the skill and marks its changeset `rejected` ("rolled back by …").
- Re-evaluates changesets queued behind it. Because they were proposed against older versions, they become `failed`.
- Leaves rule evidence counters and the skill's success posterior unchanged.

**Pinning** (`POST /v1/skills/{id}/pin` with `{"pinned": true}`) only exempts a skill from the lifecycle (active →
stale after `MYELINATE_STALE_AFTER_DAYS` unused, → archived after `MYELINATE_ARCHIVE_AFTER_DAYS`). It does **not**
freeze content. Learning can still propose changes to a pinned skill, so lock content through the approval policy.

## Rejected-edit buffer

Rejected changesets are kept with their `rejection_reason` and fed back into later prompts so the same edit isn't
proposed again:

- Curation sees the agent's 8 most recent rejected changesets.
- Evolution sees the 10 most recent rejected evolution changesets that touched that skill, plus the attempts it
  discarded during the current run.

Write useful rejection comments, because they become guidance for the learner.

## Recommended policies

| Setting | Development | Production (default) | Regulated production |
|---|---|---|---|
| `MYELINATE_AUTO_APPROVE_LOW_RISK` | `true` | `true` | `false` (every skill change reviewed) |
| `MYELINATE_AUTO_APPROVE_MEDIUM_RISK` | `true` | `false` | `false` |
| `MYELINATE_AUTO_APPROVE_MIN_EVAL_SCORE` | `0.7` | `0.7`–`0.8` | `0.8` |
| `MYELINATE_AUTO_APPROVE_MEMORIES` | `true` | `true` | `false` if memories may hold sensitive facts (review the `pending` queue) |
| `MYELINATE_CANARY_ENABLED` | `true` | `true` | `true` |
| `MYELINATE_HOLDOUT_RATE` | `0` (fast feedback) | `0.05` | `0.05`–`0.1` (evidence for audits) |
| `MYELINATE_SELF_JUDGE_UNKNOWN_OUTCOMES` | `true` | `true` | `true`, but wire real verification where possible |
| `MYELINATE_MIN_JUDGE_CONFIDENCE` | `0.7` | `0.7` | `0.8`+ |
| `MYELINATE_SHARE_LEARNED_FACTS` | as needed | `false` | `false` |
| Scopes | dev mode | `learn` for agents, `learn`+`review` for owners | as production, with reviewers distinct from agent owners |

In regulated environments, also export the audit log regularly (`GET /v1/admin/audit`; entries older than
`MYELINATE_AUDIT_RETENTION_DAYS` are removed by `myelinate purge`), and document who holds `review`. That includes who
may publish tenant-wide `agent_id="*"` skills, which only `review` can do.

See also: [Security](security.md), [Observability](observability.md), [Cost control](cost-control.md),
[Troubleshooting](../troubleshooting.md).
