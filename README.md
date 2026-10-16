# Myelinate

**Self-improving skills and memory for any AI agent framework, with governance built in.**

> *Myelin* is the sheath the brain builds around pathways that get used, and that work, so they fire faster and more
> reliably. Myelinate does the same for agents: rules that help get reinforced, rules that hurt get corrected or retired,
> and nothing changes without evidence.

A drop-in component that gives **any** agent framework a self-improvement loop: learned skills, persistent memory,
reflection, and skill evolution. Every change is governed, measured and reversible.

You attach it to Microsoft Agent Framework, OpenClaw, LangGraph, CrewAI, ADK or anything else. The agent stays
in its own framework and makes two calls per run: **recall** before it and **record** after it. Frameworks that
emit OpenTelemetry need no code at all.

The design combines the strongest published mechanisms (ACE, Kayba ACE, GEPA, SkillOpt, Trace2Skill, WikiSkill,
ReasoningBank, Mem0, Zep/Graphiti, Honcho, Hermes Agent, Letta, LangMem, Voyager, AWM). It adds what none of them
have: **live canary rollout, a holdout control group that measures causal lift, and enterprise governance**.

```
 agent (MAF · OpenClaw · LangGraph · CrewAI · ADK · custom)          OTel GenAI / OpenInference traces
   │ recall ──────────────┐            ┌──────────── record              │ (zero-code)
   ▼                      ▼            ▼                                 ▼
 ┌──────────────────────────── Myelinate (REST · MCP · OTLP) ────────────────────────────┐
 │ authN/Z → tenant isolation → rate limit → audit                                                    │
 │ RECALL   hybrid BM25+dense (RRF) × skill posterior → MMR → canary arm / holdout → stable, cacheable │
 │ RECORD   PII redaction → usage + reward credit (Beta posteriors: skills, canaries, holdout)         │
 │                         durable debounced jobs (multi-replica safe, budgets)                       │
 │ REFLECT  outcome signal (verifier > feedback > label > LLM judge) → per-run diagnosis →            │
 │          helpful/harmful tags on injected rules → memory reconcile → CURATE bounded delta ops      │
 │ EVOLVE   GEPA: Pareto parents, minibatch pre-gate, strict held-out gate, rejected-edit buffer      │
 │ CURATE   dedup-merge (KEEP remembered), stale→archived lifecycle, evidence-based nomination        │
 │ GOVERN   changeset → safety scan + lint → risk tier → policy/human → CANARY on live traffic →       │
 │          promote/reject by posterior → immutable version → rollback                                │
 └─────────────────────── SQLite (dev) · PostgreSQL (prod) · Prometheus · OpenTelemetry ─────────────┘
```

## Why it's better than each alternative

| | Hermes Agent | Kayba ACE | ACE paper | LangMem | Letta | Mem0 / Zep | Agent Lightning | **This gateway** |
|---|---|---|---|---|---|---|---|---|
| Works inside *your* framework | no (own agent) | some | no | LangChain | no (own harness) | yes (memory only) | yes (training) | **yes: REST, MCP, OTLP, adapters** |
| Learned procedures (skills) | yes | yes | playbook | prompt rewrite | yes | no | no | **yes, as rule-level bullets in SKILL.md** |
| Edits are typed deltas, not rewrites | partly | yes | yes | no | no | n/a | n/a | **yes, bounded per changeset** |
| Rule-level helpful/harmful credit | no | yes | yes | no | no | no | per-call | **yes, plus a posterior per skill** |
| Learns only from reliable signals | no | no | no | no | partly | no | needs reward | **evidence gating + judge confidence** |
| Optimises underperforming skills | GEPA (offline PR) | no | no | yes | no | no | RL/APO | **GEPA + strict gate + rejected buffer** |
| Validated on live traffic before full rollout | no | no | no | no | no | no | no | **Bayesian canary** |
| Measures causal lift | no | no | no | no | no | no | no | **holdout control group** |
| Memory with contradiction handling | char-limited files | no | no | update/delete | git files | yes (Zep: bi-temporal) | no | **reconcile + bi-temporal + profile cards** |
| Approval, versions, rollback, audit | approval gate, ledger | no | no | no | git | history | no | **changesets, versions, rollback, audit log** |
| Multi-tenant, RBAC, SSO, erasure, budgets | no | no | no | no | partly | platform | no | **yes** |

## What each mechanism is, and where it comes from

**Knowledge model**
- *Skills are sets of atomic bullets* ("When <trigger>, <action>", 15–50 words) grouped as steps, pitfalls,
  heuristics, verification, snippets and notes, and exported as agentskills.io `SKILL.md`.
  ACE's ablation: delta updates instead of whole rewrites were worth **+17 vs +3.6 points**. Rewriting causes
  "context collapse" (18k tokens fell to 122 and accuracy dropped below baseline).
- *Bounded edits per changeset* (SkillOpt's "textual learning rate"). Unbounded rewrites underperform.
- *Lint* blocks incident-diary-shaped rules (Hermes) and over-long rules. Rules must generalise, with names
  replaced by placeholders, and a broad rule can replace narrow ones only with ≥3 instances across ≥2 tasks (Kayba).

**Signals and credit**
- *Outcome hierarchy*: verifier/tests > user feedback or correction > reported outcome > LLM self-judge
  (ReasoningBank: judge accuracy of about 73% was enough).
  **Evidence gating**: runs without a reliable signal can add memories but never change skills.
  ACE and Dynamic Cheatsheet both fell *below baseline* when learning from unreliable feedback. Letta found
  verifier feedback added **+6.7 points** on top of trajectory-only learning.
- *Reflector* diagnoses each run, successes included (ACE, Trace2Skill), and tags only the rules that were
  actually injected (Kayba). Optional citation mode (`[b-xxxxxxxx]`) gives sharper attribution.
- *Beta posteriors* on every skill and canary arm, with fractional, re-creditable rewards (feedback after a judged
  outcome replaces the old credit instead of double counting).

**Improvement**
- *Curator* returns only ops for what is **missing or wrong**. An empty list is the expected answer for most batches.
  Previously rejected edits are shown to it so they aren't proposed again (SkillOpt's buffer).
- *Evolution*:
  - GEPA Pareto parent selection (+12.4 vs +6.1 for select-best in GEPA's ablation).
  - A minibatch pre-gate (b=3) before the expensive held-out evaluation.
  - A strict gate: ties lose, and the improvement must clear a margin.
  - A brevity guard that rejects children dropping more than 30% of the rules.
  - The output is a reviewable net diff.
- *Canary*: a changed proven skill is served beside the incumbent by Thompson sampling with an exploration floor.
  Assignment is sticky per session. The canary auto-promotes at P(better) ≥ 0.95 and auto-rejects at ≤ 0.05 or when
  the budget runs out. This guards against the limit every paper names: offline judges are imperfect.
- *Holdout*: by default 5% of runs get no learned skills, and the would-be-served skills are recorded so the
  comparison is like for like. `/insights` reports the lift with a credible interval: the ROI number.

**Memory**
- *Reconcile* each candidate against its nearest memories with ADD / UPDATE / SUPERSEDE / NOOP (Mem0). Exact
  duplicates cost no LLM call.
- *Bi-temporal*: superseded facts get `invalid_at` and `superseded_by` and are never deleted by learning (Zep).
  `as_of` gives point-in-time recall for audits.
- *Profile cards* per user, rebuilt in the background (Honcho peer card, Hermes USER.md). The current user's
  preferences always apply.

**Upkeep**
- *Dedup* proposes merges for near-duplicate rules. KEEP decisions are remembered and merges sum the evidence counters (ACE, Kayba).
- *Lifecycle* moves unused skills active → stale (14 d) → archived (30 d). Nothing is deleted, and pinned skills are exempt (Hermes curator).
- *Nomination*: poor posteriors or rules that keep being tagged harmful queue an evolution run. Counters nominate
  but never delete; Kayba removed auto-delete at harmful≥3 for good reason.

**Retrieval**
- BM25 plus optional embeddings, fused with RRF, with the skill posterior as a prior, then MMR for diversity (Zep, Mem0 2026).
- Progressive disclosure: the top-k skills are injected in full and the rest appear as a name index loaded with `skill_view` (Hermes).
- Injected text is ordered deterministically, so the prompt prefix caches (ACE served 91.8% of input from cache).

## Quick start

```bash
uv sync --extra maf
echo "ANTHROPIC_API_KEY=..." >> .env   # learning model (default claude-opus-5-5); read from env or .env
MYELINATE_ENV=dev uv run myelinate serve --port 8080   # dev: no credentials, tenant "dev"
open http://localhost:8080/docs
```

Without `MYELINATE_ENV=dev` the gateway is secure by default: it refuses to start without a bootstrap admin key
or OIDC configuration.

Production: `cp .env.example .env`, then `docker compose -f deploy/docker-compose.yml up -d`. That runs the gateway, a
worker pool and Postgres. Mint scoped keys with `POST /v1/admin/api-keys`.

## Plugging in

**Microsoft Agent Framework**, as a context provider:
```python
learner = SelfLearningProvider(GatewayClient("https://learner.internal", api_key="myl_..."), agent_id="support-bot",
                               verify_fn=lambda ctx: Verification(passed=ticket_resolved(ctx)))  # optional ground truth
agent = Agent(client=chat_client, instructions="...", context_providers=[learner])
```

**OpenClaw**, as a plugin ([adapters/openclaw/](adapters/openclaw/)): the `before_prompt_build` hook injects learned
context, `agent_end` records the run, and it adds the tools `skill_view` (credited to the turn), `remember`, and
`rate_last_turn` (with a correction).

**Zero code** for any framework that emits OpenTelemetry GenAI spans (MAF, ADK, Strands, Pydantic AI, OpenClaw
diagnostics-otel, or LangGraph/CrewAI through OpenInference):
```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://learner.internal/v1/traces
OTEL_EXPORTER_OTLP_PROTOCOL=http/json
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer myl_...,X-Agent-Id=support-bot"
```
`gen_ai.skill.name` spans credit skills automatically. Set the span attributes `myelinate.outcome` /
`myelinate.verification.passed` to add labels. Recall still needs one call, or the MCP tool, for the agent to
*use* what was learned.

**Any MCP client** (Claude, Hermes, Cursor...): `https://learner.internal/mcp`, with the tools `recall`, `skill_view`,
`remember`, `record_episode`, `feedback` and `ask`.

**Anything else** (LangGraph, CrewAI, OpenAI Agents SDK...):
```python
async with learner.turn(task, user_id="u1") as t:
    out = await run_my_agent(system=BASE + t.instructions, task=task)
    t.add_message("assistant", out)
    t.verify(passed=tests_ok, details=test_log)
```

**Bring existing skills**: `POST /v1/skills/import` with any SKILL.md (Anthropic, Hermes, agentskills.io). Bulk-import
history with `POST /v1/episodes:batch`, then `POST /v1/agents/{id}/replay` to warm-start learning offline (ACE's
offline warm-up lifted its online score from 56.1 to 59.5).

## API

| Area | Endpoints |
|---|---|
| Loop | `POST /v1/recall`, `POST /v1/episodes`, `POST /v1/episodes:batch`, `POST /v1/feedback`, `POST /v1/traces` (OTLP), `POST /v1/ask` |
| Skills | `GET /v1/skills`, `GET /v1/skills/{ref}`, `GET …/SKILL.md`, `GET …/versions`, `POST /v1/skills`, `POST /v1/skills/import`, `POST …/rollback`, `POST …/pin`, `POST …/evolve` |
| Memory | `GET|POST /v1/memories` (`as_of`, `include_history`), `POST …/approve`, `DELETE /v1/memories/{id}`, `GET /v1/profiles/{agent}/{user}` |
| Governance | `GET /v1/changesets`, `POST /v1/changesets/{id}/review` (`skip_canary`), `POST /v1/agents/{id}/reflect` / `curate` / `replay`, `GET /v1/agents/{id}/insights`, `GET /v1/agents/{id}/export` (JSONL for RL / fine-tuning) |
| Admin | `POST|DELETE /v1/admin/api-keys`, `GET /v1/admin/audit`, `GET /v1/admin/usage`, `DELETE /v1/admin/users/{id}` |
| Ops | `/healthz`, `/readyz`, `/metrics`, `/mcp`, `/docs` |

## Enterprise controls

- **Identity:** tenant API keys (hashed, revocable) or OIDC (Entra ID, Okta, Auth0, Keycloak). Scopes are `learn`, `review` and `admin`.
- **Isolation:** every query is tenant-scoped. Knowledge is scoped per agent, with tenant-shared skills under `agent_id="*"`.
- **Safety:**
  - PII and secrets are redacted before storage.
  - Every changeset is scanned for secrets, prompt injection and oversize content. Flagged changes never auto-apply.
  - Risk tiers: additive = low, edits = medium, removals and routing changes = high (always human).
- **Change control:**
  - Immutable version snapshots and optimistic concurrency (stale changesets fail instead of clobbering).
  - Canary before full rollout, and one-call rollback.
  - The rejected-change history is kept and learned from.
- **Privacy:**
  - Right to erasure.
  - Episode retention (`myelinate purge`).
  - Hard memory delete for data-subject requests. Learning itself never hard-deletes.
- **Cost:** a per-tenant daily token budget. Over-budget jobs are deferred, not failed. Usage is broken down by purpose for chargeback.
- **Reliability:**
  - A durable job queue with leases, heartbeats, retries and debounce, safe across replicas. Only one job per agent
    reflects at a time.
  - Atomic SQL counter updates, so concurrent runs never lose credit.
  - Idempotency keys (set by the SDKs and derived from the OTel trace id), so retried uploads never double count.
  - Adapters fail open, so a gateway outage never breaks the agent.
  - Canaries that can't reach a verdict expire and keep the incumbent. Queued changes are re-evaluated, never
    silently rebased.
- **Observability:** Prometheus metrics, including learning-cost and canary-verdict counters. JSON logs carry request and tenant IDs, and OTel tracing is available.

## Layout

```
src/myelinate/
  engine.py       facade: recall / record / credit / feedback / ask / insights / export
  learning.py     outcome signal, reflector, evidence gating, curator
  evolution.py    GEPA-style evolution with gates
  governance.py   changesets, policy, canary experiments, apply, versions, rollback
  memory.py       reconcile, bi-temporal store, profile cards
  maintenance.py  dedup, lifecycle, nomination, profile refresh
  skills.py       delta ops, simulation, diff, SKILL.md render/parse, lint
  experiments.py  Beta posteriors, Thompson sampling, lift
  retrieval.py    BM25 + dense RRF + MMR      embeddings.py  optional /v1/embeddings client
  otel.py         OTLP → episodes             prompts.py     role prompts + strict schemas
  core.py         metered LLM, budgets, jobs, audit           worker.py  durable job worker
  gateway/        FastAPI app, auth/RBAC, MCP server          adapters/  maf.py, generic.py
adapters/openclaw TypeScript plugin + reusable TS client
tests/            51 tests: loop, gating, tagging, canary, GEPA, memory, dedup, lift, OTel, import, auth, MCP, budget,
                  concurrency, idempotency, SDK request shape, regressions
```

## Verification status

| Check | Result |
|---|---|
| Test suite (51 tests) | Passes on SQLite and on PostgreSQL 17 (`MYELINATE_TEST_DATABASE_URL=postgresql+asyncpg://… uv run pytest`), with 5 repeated runs and no flakes |
| Microsoft Agent Framework adapter | Runs inside a real `agent_framework` 1.22 `Agent` (test) |
| OpenClaw plugin | Type-checks against the real `openclaw@2026.10.4` SDK. Installs, enables, passes `plugins doctor`, and its hooks and tools register in the real runtime (`inspect --runtime`). **Real agent turns with `anthropic/claude-opus-5-5` close the loop:** (1) the agent followed an injected skill exactly; (2) a user correction given in chat reached the gateway through `rate_last_turn`; (3) Claude reflected and proposed a rule update, which policy held for review; (4) after approval, a brand-new session produced the corrected format |
| Docker | `docker build` succeeds (415 MB, non-root). `docker compose` (gateway, 2 workers, Postgres) reaches healthy on every service. A failed production migration recorded via the API was learned by a worker replica with real Claude into a correct `postgres-schema-migration` skill stored in Postgres |
| Claude request path | Real Anthropic SDK (1.15) with a mocked transport: request shape, structured output, fallbacks, usage metering, refusal / max_tokens / 429 / 400 handling. Every schema is checked to be strict |
| **Live Claude (`claude-opus-5-5`)** | Full pipeline against the real API: outcome judging, reflection, curation (a skill with 3 well-formed rules from one failed deploy), memory reconciliation, learning from a user correction, GEPA evolution (one run accepted at judge 0.46 → 0.80, one rejected for gaining too little), dedup merge, profile card, ask. About $0.44 upper bound for the whole run (31 calls) |
| Production mode | The built package boots with `MYELINATE_ENV=prod` on PostgreSQL. Verified: auth, scopes, recall, idempotent record, input limits, MCP tool calls, OTLP ingestion, audit, metrics |
| Static analysis | ruff clean. Pyright reports only known false positives (FastAPI `Annotated` dependencies, SQLAlchemy `rowcount`) |

**Not covered here:** load and performance testing, Kubernetes manifests (only Compose ships), and Entra ID / Okta SSO
against a real identity provider (JWT validation is implemented, but was not tested against one).

## Honest limits

- **Learning quality is bounded by signal quality.** Wire `verify_fn` / `t.verify()` / feedback to real checks where
  you can. Without them the system stays conservative (memories only).
- **The LLM judge used in evolution is a proxy.** That is why changed proven skills are canaried on live traffic. Canaries
  need traffic: with few runs per skill, keep `AUTO_APPROVE_MEDIUM_RISK=false` and review.
- **Migrations.** Tables are created at startup. Add Alembic before your first production schema change.
- **Scale.** Lexical retrieval over a few thousand skills per agent is fine. Beyond that, configure embeddings and move them
  to pgvector. The rate limiter is per process; put a shared limiter in front of multiple replicas.
- **OTLP** is accepted as JSON (`http/json`). Protobuf needs a collector in front.
- **The OpenClaw plugin** targets `openclaw@2026.10.4` (Node ≥24.16 <25 or ≥26.1). Re-run `npx tsc` against newer
  SDK versions when upgrading.
