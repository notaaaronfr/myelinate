# FAQ

**Does Myelinate replace my agent framework?**
No. Your agent keeps running in its own framework, and Myelinate adds a learning layer around it: recall before a run,
record after. It never calls your agent's model.

**Which LLM does it use, and does my agent have to use Claude?**
Learning (reflection, curation, judging, evolution) uses Claude through the Anthropic API (`MYELINATE_LLM_MODEL`,
default `claude-opus-5-5`). Your agent can use any model. With `MYELINATE_LLM_PROVIDER=none`, Myelinate still stores
and serves knowledge but doesn't learn on its own.

**Does it add latency to my agent?**
`recall` and `record` are a few database queries and never call an LLM. All learning happens in background workers.

**What does learning cost?**
It depends on traffic and settings. As a reference point, a full live pipeline run (reflection, curation, memory,
evolution with judging, dedup, profile and ask; 31 calls) cost at most about $0.44 on `claude-opus-5-5`. Cap spend
per tenant with `MYELINATE_DAILY_TOKEN_BUDGET`. See [Cost control](operations/cost-control.md).

**What if it learns something wrong?**
Several layers guard against it:
- Skills only change on reliable evidence.
- Risky changes wait for review.
- Changes to proven skills are canaried on live traffic.
- Rules that keep being marked harmful get evolved.
- Every version can be restored with `POST /v1/skills/{id}/rollback`.

**What if Myelinate is down?**
The adapters fail open: your agent runs without learned context, and a warning is logged.

**Can I bring my existing skills?**
Yes. `POST /v1/skills/import` accepts `SKILL.md` files in the Anthropic, Hermes or agentskills.io format, and
`GET /v1/skills/{name}/SKILL.md` exports them back.

**Can several agents share knowledge?**
Agents with the same `agent_id` share everything. Skills published under `agent_id="*"` are visible to every agent in
the tenant. Only reviewers can publish there (`POST /v1/skills` or `/v1/skills/import` with `review` scope), so one
agent can't push knowledge into every other agent.

**Is my data sent anywhere?**
Episodes, after PII redaction, are sent to the configured LLM provider for learning. Nothing else leaves your
deployment. See [Data and privacy](operations/data-and-privacy.md).

**How do I know it's actually helping?**
Look at `learning_lift` in `GET /v1/agents/{agent_id}/insights`. It compares success rates between runs that got
learned skills and a holdout group that didn't, on comparable tasks, with a credible interval.

**Is there a .NET SDK?**
Not yet. .NET agents (including Microsoft Agent Framework for .NET) can use the [MCP endpoint](integrations/mcp.md)
or the [REST API](reference/api.md).
