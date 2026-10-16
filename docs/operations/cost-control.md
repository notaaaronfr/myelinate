# Cost control

Myelinate's own cost is mostly **learning-side LLM calls** made by background workers. The hot path (recall, record,
feedback, OTLP ingestion) never calls the LLM, so your agents' latency and token bills are unaffected, apart from the
learned context added to their prompts.

## Where the spend comes from

Every call is metered under a *purpose*, visible in `GET /v1/admin/usage`, in `/insights` and in the
`myelinate_llm_*` metrics.

| Purpose | When | Volume driver |
|---|---|---|
| `judge_outcome` | During reflection, for episodes with no verification, feedback or outcome label (`MYELINATE_SELF_JUDGE_UNKNOWN_OUTCOMES`) | One per unlabelled episode |
| `reflect` | Each reflection job; one call per unreflected episode (up to 20 per job) | Episodes that trigger reflection |
| `curate` | Once per reflection job that has evidence-bearing reflections | Reflection jobs |
| `memory` | Reconciling candidate memories that resemble existing ones. Exact duplicates and entirely new facts cost nothing. | Memory churn |
| `dedup` | During the periodic curate job, when near-duplicate rules exist | Library size and growth |
| `profile` | Rebuilding user profile cards whose preferences changed (during curation) | Active users with preferences |
| `judge_skill` | Evolution: scoring a skill version against recorded episodes | Underperforming skills |
| `evolve` | Evolution: proposing edits (once per iteration) | Underperforming skills |
| `ask` | `POST /v1/ask` and the MCP `ask` tool, synchronously | Your callers |

Evolution is the most expensive single job. With defaults (`MYELINATE_EVOLVE_EVAL_SAMPLE=10`,
`MYELINATE_EVOLVE_ITERATIONS=4`, `MYELINATE_EVOLVE_MINIBATCH=3`), one run can make up to about 80 calls: the seed
scored on up to 10 held-out episodes, then per iteration one `evolve` call and up to 3 + 3 + 10 `judge_skill` calls.
Scores are cached per candidate, so real runs usually make fewer.

### A measured data point

A full live pipeline run against `claude-opus-5-5` made **31 calls** and cost **about $0.44 (upper bound)**. It covered
outcome judging, reflection, curation, memory reconciliation, learning from a user correction, two evolution
attempts, a dedup merge, a profile card and an ask. Treat it as an order of magnitude (a little over 1 cent per call
on average at default effort), not a prediction. Your cost depends on transcript length, effort and how often
reflection triggers.

## The daily token budget

`MYELINATE_DAILY_TOKEN_BUDGET` (default 5,000,000; `0` means unlimited) caps learning-side tokens **per tenant per UTC
day**.

- Before every LLM call, today's usage for the tenant (input + output across all purposes) is compared with the budget.
  Input counts uncached tokens, cache writes and cache reads, so the budget is conservative relative to billed cost.
- **Background jobs over budget are deferred, not failed.** The job goes back to `pending` with a start time one hour
  later, and the attempt isn't counted. Learning resumes after the UTC day rolls over. Nothing is lost.
- Calls already in flight finish, so concurrent jobs can overshoot the budget slightly.
- **Synchronous calls aren't deferred.** `POST /v1/ask` returns `429` with `Retry-After: 3600` while the tenant is
  over budget. `POST /v1/memories` still succeeds: new memories are added, and any that resemble an existing memory
  (which would need the LLM to reconcile) are stored as `pending` for review.
- Embedding calls aren't metered or budgeted.

To find deferred jobs:

```sql
SELECT tenant_id, kind, run_after FROM jobs
WHERE status = 'pending' AND last_error LIKE '%daily learning token budget%';
```

## Usage reporting

```bash
curl -s "$GW/v1/admin/usage" -H "Authorization: Bearer $ADMIN_KEY"
```

```json
[{"day": "2026-10-16", "purpose": "reflect", "calls": 42, "input_tokens": 512000, "output_tokens": 61000},
 {"day": "2026-10-16", "purpose": "curate", "calls": 9, "input_tokens": 88000, "output_tokens": 12000}]
```

This is `admin`-scoped and per tenant, newest days first, up to 500 rows. Use it for chargeback, multiplying by your
model's per-token prices. `GET /v1/agents/{id}/insights` includes a 30-day `tenant_learning_cost_30d` summary.
As the name says, it covers the whole tenant, not just the agent. For fleet-wide trends, use
`sum by (purpose) (increase(myelinate_llm_tokens_total[1d]))`.

## Knobs to reduce cost

| Knob | Default | Effect |
|---|---|---|
| `MYELINATE_LLM_MODEL` | `claude-opus-5-5` | The largest lever. A smaller model costs less per call. Learning quality, especially curation and evolution, may drop, so compare `/insights` lift before and after. |
| `MYELINATE_LLM_EFFORT` | `medium` | `low` reduces thinking and output tokens; `high`/`xhigh`/`max` increase them |
| `MYELINATE_LLM_MAX_TOKENS` | 16000 | Caps output per call. Too low causes "output truncated" failures. |
| `MYELINATE_REFLECT_EVERY_N_EPISODES` | 5 | Raise to reflect less often on routine successes |
| `MYELINATE_REFLECT_MIN_TOOL_CALLS` | 5 | Raise so long tool-heavy runs don't each trigger a reflection |
| `MYELINATE_REFLECT_ON_FAILURE` | `true` | Failures are the most informative runs, so keep this on if you can |
| `MYELINATE_REFLECT_DEBOUNCE_SECONDS` | 20 | Longer debounce batches more episodes per job |
| `MYELINATE_SELF_JUDGE_UNKNOWN_OUTCOMES` | `true` | `false` removes `judge_outcome` calls. Unlabelled runs then can't change skills. Better: send real outcomes or verification. |
| `MYELINATE_CURATE_EVERY_N_EPISODES` | 25 | Raise to run dedup and profile rebuilds less often |
| `MYELINATE_EVOLVE_MIN_USES` | 10 | Raise so only well-used skills are evolved |
| `MYELINATE_EVOLVE_MAX_SUCCESS_RATE` | 0.7 | Lower so only clearly poor skills are evolved |
| `MYELINATE_EVOLVE_ITERATIONS`, `MYELINATE_EVOLVE_EVAL_SAMPLE`, `MYELINATE_EVOLVE_MINIBATCH` | 4, 10, 3 | Smaller values mean cheaper evolution runs but a noisier gate |
| `MYELINATE_MAX_OPS_PER_CHANGESET` | 6 | Bounds the size of each edit (and output tokens) |
| `MYELINATE_DAILY_TOKEN_BUDGET` | 5,000,000 | Hard daily ceiling per tenant |
| `MYELINATE_LLM_PROVIDER=none` | | No learning-side LLM spend at all; explicit memories and human-authored skills only |

Tips:

- **Send real signals.** Verification results and user feedback replace judge calls and make every other call more
  useful.
- **Keep transcripts lean.** Reflection prompts include messages (each clipped to 3000 characters) and tool calls
  (clipped). Don't send large binary-ish tool outputs.
- System prompts are stable per purpose and sent with prompt caching, so repeated calls reuse cached prefixes.
- `POST /v1/agents/{id}/replay` re-reflects up to `limit` stored episodes, with a full reflection cost for each. Use it
  deliberately.

The full settings table is in the [configuration reference](../reference/configuration.md).

See also: [Observability](observability.md), [Governance](governance.md), [Troubleshooting](../troubleshooting.md).
