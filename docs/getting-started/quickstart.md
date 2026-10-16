# Quickstart

This takes about five minutes. You run the gateway locally, give it a skill, and walk through one learning loop with
`curl`. No agent framework or API key is needed yet.

## 1. Run the gateway

=== "Python (uv)"

    ```bash
    git clone https://github.com/notaaaronfr/myelinate.git
    cd myelinate
    uv sync
    MYELINATE_ENV=dev uv run myelinate serve --port 8080
    ```

=== "Python (pip)"

    ```bash
    pip install "myelinate @ git+https://github.com/notaaaronfr/myelinate"
    MYELINATE_ENV=dev myelinate serve --port 8080
    ```

=== "Docker"

    ```bash
    docker run --rm -p 8080:8080 -e MYELINATE_ENV=dev ghcr.io/notaaaronfr/myelinate:0.2.0
    ```

`MYELINATE_ENV=dev` turns authentication off and puts everything in a tenant called `dev`. Use it only on your own
machine. Without it, Myelinate refuses to start until you configure credentials (see
[Security and SSO](../operations/security.md)).

Check it's up, then open the interactive API docs at <http://localhost:8080/docs>:

```bash
curl http://localhost:8080/healthz
# {"status":"ok"}
```

## 2. Give it a skill

A skill is a named procedure made of short rules. You can import any existing `SKILL.md` (Anthropic, Hermes or
agentskills.io format) or write one:

```bash
curl -X POST http://localhost:8080/v1/skills/import -H 'content-type: application/json' -d '{
  "agent_id": "support-bot",
  "markdown": "---\nname: refund-request\ndescription: Handle a customer refund request\n---\n## Steps\n1. Look up the order and confirm it is within the 30-day refund window.\n2. If the item was damaged, ask for a photo before approving.\n## Pitfalls\n- Never promise a refund before the order lookup succeeds."
}'
# {"changeset_id":"cs_...","status":"applied"}
```

## 3. Recall before a run

Your agent calls `recall` with the task. Myelinate returns the relevant skills and memories as ready-to-use
instructions for your system prompt:

```bash
curl -X POST http://localhost:8080/v1/recall -H 'content-type: application/json' -d '{
  "scope": {"agent_id": "support-bot", "user_id": "alice"},
  "query": "customer wants their money back for a broken blender"
}'
```

The response contains a `recall_id` and `instructions` like:

```text
## Learned skills
Rules learned from previous runs. Follow them when they apply; if one is wrong for this situation, don't follow it.

### refund-request
Handle a customer refund request

#### Steps
- Look up the order and confirm it is within the 30-day refund window.
- If the item was damaged, ask for a photo before approving.
#### Pitfalls
- Never promise a refund before the order lookup succeeds.
```

The query never mentioned "refund", yet the skill was found: retrieval matches on meaning-bearing words, word forms
and common abbreviations. Configure embeddings for fuller semantic matching.

## 4. Record after the run

Send what happened, with the `recall_id`, so Myelinate can credit the skills that were used:

```bash
curl -X POST http://localhost:8080/v1/episodes -H 'content-type: application/json' -d '{
  "scope": {"agent_id": "support-bot", "user_id": "alice"},
  "recall_id": "<recall_id from step 3>",
  "task": "Refund a broken blender",
  "messages": [
    {"role": "user", "content": "My blender arrived broken, I want a refund"},
    {"role": "assistant", "content": "Sorry! Could you send a photo of the damage?"}
  ],
  "outcome": "success"
}'
# {"episode_id":"ep_...","reflection_scheduled":false}
```

The outcome is the most important field for learning. Use `success`, `failure` or `partial` when you know it, and
attach a `verification` object (`{"passed": true, "details": "..."}`) when tests or checks ran. Users can also rate a
run afterwards:

```bash
curl -X POST http://localhost:8080/v1/feedback -H 'content-type: application/json' -d '{
  "episode_id": "<episode_id from above>", "score": 1.0, "comment": "Great handling"
}'
```

## 5. See what it learned

```bash
curl http://localhost:8080/v1/agents/support-bot/insights
```

This shows each skill's uses and success rate (with a credible interval), running canary experiments, rules that keep
being marked harmful, the learning lift versus the control group, and learning cost.

## 6. Turn on learning

Until now the gateway only stored and served knowledge. To have it *learn* (reflect on runs, write and improve
skills, reconcile memories), give it an Anthropic API key and restart:

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
MYELINATE_ENV=dev uv run myelinate serve --port 8080
```

Record a few runs with real outcomes, especially failures with verification details. After every few episodes (or
right after a failure) Myelinate reflects and proposes changes. Additive changes apply automatically; edits to
existing rules wait for review in `GET /v1/changesets`. See [Governance](../operations/governance.md).

## Next steps

- Connect your agent: [Microsoft Agent Framework](../integrations/microsoft-agent-framework.md),
  [OpenClaw](../integrations/openclaw.md), [MCP clients](../integrations/mcp.md),
  [OpenTelemetry (zero code)](../integrations/opentelemetry.md) or
  [LangGraph, CrewAI and others](../integrations/other-frameworks.md).
- Understand how learning works: [Concepts](concepts.md).
- Deploy it: [Docker](../deployment/docker.md) or [Kubernetes](../deployment/kubernetes.md).
