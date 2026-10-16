# @myelinate/openclaw-plugin

Self-improving skills and memory for [OpenClaw](https://openclaw.ai) agents, through the
[Myelinate](https://github.com/notaaaronfr/myelinate) gateway.

- **Before each turn** (`before_prompt_build`) it injects the learned skills, memories and user profile that apply.
- **After each turn** (`agent_end`) it records the run, so the gateway can learn from it.
- **Tools:** `skill_view`, `remember` and `rate_last_turn` (with a correction).
- **Fails open:** if the gateway is unreachable, turns run normally.

```bash
openclaw plugins install @myelinate/openclaw-plugin
openclaw config set plugins.entries.myelinate.config.gatewayUrl https://myelinate.internal
openclaw config set plugins.entries.myelinate.hooks.allowConversationAccess true
export MYELINATE_API_KEY=myl_...        # a tenant key with the `learn` scope
openclaw plugins enable myelinate && openclaw plugins doctor
```

Requires Node ≥24.16 <25 or ≥26.1 and OpenClaw 2026.10 or later. Full guide:
<https://notaaaronfr.github.io/myelinate/integrations/openclaw/>

MIT licensed.
