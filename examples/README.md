# Examples

Runnable examples for integrating an agent with Myelinate. All of them talk to a gateway at
`http://localhost:8080` unless `MYELINATE_URL` is set, and send `MYELINATE_API_KEY` as a bearer token when it is set.

Start a local dev gateway first (no credentials, tenant `dev`, no LLM needed for these examples):

```bash
uv sync --extra server
MYELINATE_ENV=dev MYELINATE_LLM_PROVIDER=none uv run myelinate serve --port 8080
```

With `MYELINATE_LLM_PROVIDER=none` the loop works (recall, record, feedback, credit) but background reflection has no
model, so nothing new is learned. Drop it and set `ANTHROPIC_API_KEY` to see learning happen.

| File | What it shows | Run |
|---|---|---|
| [`quickstart.sh`](quickstart.sh) | The REST loop with curl: import a SKILL.md, recall, record with verification, feedback, insights. Needs `curl` and `jq`. | `./examples/quickstart.sh` |
| [`generic_agent.py`](generic_agent.py) | The framework-agnostic `SelfLearning` adapter around a stand-in agent function. No LLM needed. | `uv run python examples/generic_agent.py` |
| [`maf_agent.py`](maf_agent.py) | Microsoft Agent Framework with `SelfLearningProvider`, `verify_fn`, `outcome_fn` and feedback. Uses Claude via `agent-framework-anthropic` when `ANTHROPIC_API_KEY` is set, otherwise a scripted stand-in client. | `uv sync --extra server --extra maf && uv run python examples/maf_agent.py` |
| [`openclaw-config.json5`](openclaw-config.json5) | The plugin entry for OpenClaw. | `openclaw config patch --file examples/openclaw-config.json5` |

Notes:

- Importing a skill needs the `review` scope; everything else needs `learn`.
- About 5% of recalls are holdout runs that intentionally return no instructions (`MYELINATE_HOLDOUT_RATE`). If an
  example prints no learned context, run it again.
- To use the real Claude client in `maf_agent.py`: `uv pip install agent-framework-anthropic` (or
  `uv run --with agent-framework-anthropic ...`) and set `ANTHROPIC_API_KEY`. `ANTHROPIC_MODEL` overrides the model.

Integration guides: [docs/integrations](../docs/integrations/overview.md).
