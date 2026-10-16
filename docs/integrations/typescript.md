# TypeScript

The OpenClaw plugin ships a small, dependency-free REST client,
[`adapters/openclaw/client.ts`](https://github.com/notaaaronfr/myelinate/blob/main/adapters/openclaw/client.ts). It
has no OpenClaw imports, so you can use it from any TypeScript or JavaScript agent: Mastra, LangGraph.js, the Vercel
AI SDK, or plain code.

## Install

The client is not published as a separate npm package. Copy `client.ts` into your project (it is one file, MIT
licensed):

```bash
curl -fsSLo src/myelinate.ts \
  https://raw.githubusercontent.com/notaaaronfr/myelinate/main/adapters/openclaw/client.ts
```

It needs a runtime with global `fetch`, `AbortSignal.timeout` and `crypto.randomUUID`: Node 20+, Bun, Deno, or edge
runtimes.

The snippets import it as `./myelinate`, which suits bundlers and `tsx`. When Node runs TypeScript natively or you
compile with `moduleResolution: NodeNext`, import `./myelinate.ts` and enable `allowImportingTsExtensions` (as the
plugin's own `tsconfig.json` does).

## API

```typescript
import { GatewayClient } from "./myelinate";

const learner = new GatewayClient(
  process.env.MYELINATE_URL ?? "http://localhost:8080",   // base URL; a path prefix is kept
  process.env.MYELINATE_API_KEY,                          // optional bearer key (myl_...)
  4000,                                                   // timeout per request in ms (default 4000)
);
```

| Method | REST call |
|---|---|
| `recall(scope, query, sessionId?)` | `POST /v1/recall` → `{recall_id, instructions, skills, memories, ...}` |
| `record(episode)` | `POST /v1/episodes` → `{episode_id, reflection_scheduled}`. Adds an `idempotency_key` unless you pass one. |
| `feedback(episodeId, score, comment?, correction?)` | `POST /v1/feedback` |
| `latestEpisode(agentId, sessionId)` | `GET /v1/episodes/latest` → episode id or `null` |
| `remember(scope, content, kind?)` | `POST /v1/memories`; `kind` is `"fact"` (default), `"preference"` or `"lesson"` |
| `skillMarkdown(agentId, name)` | `GET /v1/skills/{name}/SKILL.md` → text or `null` |
| `ask(scope, question)` | `POST /v1/ask` → `{answer, evidence}` |

`scope` is `{ agent_id: string; user_id?: string | null }`. Requests retry twice on network errors, 5xx and 429.
Errors are thrown as `Error("myelinate <status>: <body>")`.

The client throws on failure; it does not fail open by itself. Wrap recall so the agent still runs when the gateway
is down, and don't await record on the response path.

## A reusable wrapper

The examples below share this helper. It recalls before the run, fails open, and records after, including failures:

```typescript
import { GatewayClient, type Outcome, type Scope } from "./myelinate";

const learner = new GatewayClient(process.env.MYELINATE_URL ?? "http://localhost:8080", process.env.MYELINATE_API_KEY);

export interface TurnResult {
  text: string;
  outcome?: Outcome;                                       // default "unknown"
  verification?: { passed: boolean; details?: string };    // ground truth, if you have it
  toolCalls?: { name: string; arguments?: unknown; result?: string | null; error?: string | null }[];
}

export async function learningTurn(
  scope: Scope, task: string, sessionId: string,
  run: (instructions: string) => Promise<TurnResult>,
): Promise<TurnResult & { episode: Promise<string | null> }> {
  let recallId: string | null = null;
  let instructions = "";
  try {
    const r = await learner.recall(scope, task, sessionId);
    recallId = r.recall_id;
    instructions = r.instructions;
  } catch (err) {
    console.warn("[myelinate] recall failed, running without learned context:", err);
  }

  const started = Date.now();
  let result: TurnResult | undefined;
  let error: string | null = null;
  try {
    result = await run(instructions);
    return { ...result, episode: record() };
  } catch (err) {
    error = String(err);
    void record();
    throw err;
  }

  function record(): Promise<string | null> {
    return learner.record({
      scope, session_id: sessionId, recall_id: recallId, task,
      messages: [{ role: "user", content: task }, ...(result ? [{ role: "assistant", content: result.text }] : [])],
      tool_calls: result?.toolCalls ?? [],
      outcome: error ? "failure" : (result?.outcome ?? "unknown"),
      verification: result?.verification ? { ...result.verification, source: "verifier" } : null,
      error, duration_ms: Date.now() - started, framework: "my-ts-app",
    }).then((out) => out.episode_id)
      .catch((err) => { console.warn("[myelinate] record failed:", err); return null; });
  }
}
```

`episode` resolves to the episode id once the upload finishes; use it for feedback:

```typescript
const turn = await learningTurn(scope, task, sessionId, run);
const episodeId = await turn.episode;
if (episodeId) await learner.feedback(episodeId, 0.0, "wrong region", "Deploy to eu-west-1, not us-east-1.");
```

## Vercel AI SDK

```typescript
import { generateText } from "ai";
import { anthropic } from "@ai-sdk/anthropic";
import { learningTurn } from "./learning";

const scope = { agent_id: "docs-assistant", user_id: userId };

const turn = await learningTurn(scope, question, sessionId, async (learned) => {
  const { text } = await generateText({
    model: anthropic("claude-opus-5-5"),
    system: `You answer questions about our API.\n\n${learned}`,
    prompt: question,
  });
  return { text };
});
```

## Mastra

Mastra's `generate` accepts an extra `system` message per call, so the agent definition stays unchanged:

```typescript
import { Agent } from "@mastra/core/agent";
import { learningTurn } from "./learning";

const support = new Agent({
  name: "support",
  instructions: "You are a support agent for an online shop.",
  model: "anthropic/claude-opus-5-5",
});

const turn = await learningTurn({ agent_id: "support", user_id: customerId }, message, threadId,
  async (learned) => {
    const res = await support.generate(message, { system: learned });
    return { text: res.text };
  });
```

## LangGraph.js / LangChain.js

```typescript
import { createAgent } from "langchain";
import { learningTurn } from "./learning";

const turn = await learningTurn({ agent_id: "research-bot" }, task, threadId, async (learned) => {
  const agent = createAgent({
    model: "anthropic:claude-opus-5-5",
    tools: myTools,
    systemPrompt: `You are a research assistant.\n\n${learned}`,
  });
  const result = await agent.invoke({ messages: [{ role: "user", content: task }] });
  return { text: String(result.messages.at(-1)?.content ?? "") };
});
```

## Exposing skill_view and remember as tools

Give the model the same two tools the Python adapters add, using your framework's tool helper. With the Vercel AI
SDK, for example:

```typescript
import { tool } from "ai";
import { z } from "zod";

const loaded: string[] = [];   // pass as skills_loaded when recording, so loaded skills get credit

const learningTools = {
  skill_view: tool({
    description: "Load the full instructions of a learned skill by name (listed under 'Other learned skills').",
    inputSchema: z.object({ name: z.string() }),
    execute: async ({ name }) => {
      const md = await learner.skillMarkdown("docs-assistant", name);
      if (md) loaded.push(name);
      return md ?? `No skill named '${name}'.`;
    },
  }),
  remember: tool({
    description: "Save one short durable memory: 'fact', 'preference' (about the user) or 'lesson'. Never secrets.",
    inputSchema: z.object({ content: z.string(), kind: z.enum(["fact", "preference", "lesson"]).optional() }),
    execute: async ({ content, kind }) => {
      await learner.remember({ agent_id: "docs-assistant", user_id: userId }, content, kind ?? "fact");
      return "Saved.";
    },
  }),
};
```

To credit loaded skills, include `skills_loaded: loaded` in the episode you record.

## OpenTelemetry instead

If your TypeScript agent already emits OpenTelemetry GenAI spans, point the OpenTelemetry JavaScript OTLP/HTTP trace
exporter (JSON or protobuf) straight at the gateway for recording; see [OpenTelemetry](opentelemetry.md). You still need `recall` for the
agent to use what was learned.
