// OpenClaw plugin: gives any OpenClaw agent a Hermes-style learning loop backed by the Myelinate gateway.
//
//   before_prompt_build -> recall learned skills + memories, prepend them to the turn's context
//   agent_end           -> record the episode (messages, tool calls, outcome) for reflection
//   tools               -> skill_view, remember, rate_last_turn
//
// Learning never blocks or breaks a turn: every gateway call fails open with a warning.
// Enable in openclaw config:  plugins.entries.myelinate = { enabled: true, hooks: { allowConversationAccess: true },
//                                                             config: { gatewayUrl: "https://..." } }

import { Type } from "@sinclair/typebox";
import { definePluginEntry, type OpenClawPluginToolContext } from "openclaw/plugin-sdk/plugin-entry";
import { GatewayClient, type EpisodeIn, type Outcome } from "./client.ts";

interface PluginConfig {
  gatewayUrl: string;
  apiKey?: string;
  agentId?: string;
  perUserMemory?: boolean;
  timeoutMs?: number;
}

interface TurnState {
  recallId?: string;
  startedAt: number;
  task: string;
  userId?: string;
  skillsLoaded: string[];
}

// OpenClaw's typed hook events vary by harness/version; read fields defensively.
type AnyRecord = Record<string, any>;

const MAX_TURNS_TRACKED = 5000;

function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((c: AnyRecord) => (typeof c === "string" ? c : c?.type === "text" ? c.text : ""))
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

function toolCallsOf(messages: AnyRecord[]): EpisodeIn["tool_calls"] {
  const calls = new Map<string, NonNullable<EpisodeIn["tool_calls"]>[number]>();
  for (const m of messages) {
    for (const c of Array.isArray(m?.content) ? m.content : []) {
      if (c?.type === "toolCall" || c?.type === "tool_use") {
        calls.set(c.id ?? String(calls.size), { name: c.name, arguments: c.arguments ?? c.input });
      }
    }
    if (m?.role === "toolResult" || m?.role === "tool") {
      const call = calls.get(m.toolCallId ?? m.tool_call_id);
      if (call) {
        const out = textOf(m.content).slice(0, 4000);
        if (m.isError) call.error = out;
        else call.result = out;
      }
    }
  }
  return [...calls.values()];
}

export default definePluginEntry({
  id: "myelinate",
  name: "Myelinate",
  description: "Learned skills, memory and self-improvement via the Myelinate gateway.",
  register(api) {
    const cfg = (api.pluginConfig ?? {}) as unknown as Partial<PluginConfig>;
    const agentId = cfg.agentId ?? "openclaw";
    const client = new GatewayClient(
      cfg.gatewayUrl ?? process.env.MYELINATE_URL ?? "http://localhost:8080",
      cfg.apiKey ?? process.env.MYELINATE_API_KEY,
      cfg.timeoutMs ?? 4000,
    );
    const log = api.logger ?? console;
    const turns = new Map<string, TurnState>();
    const lastEpisode = new Map<string, string>();

    const keyOf = (ctx: { sessionKey?: string; runId?: string }) => String(ctx?.sessionKey ?? ctx?.runId ?? "default");
    const userOf = (ctx: AnyRecord): string | undefined =>
      cfg.perUserMemory === false ? undefined : (ctx?.requesterSenderId ?? ctx?.senderId ?? undefined);

    api.on("before_prompt_build", async (event, ctx) => {
      // currentUserMessage is authoritative when present (even empty); `prompt` is the legacy fallback.
      const task = event.currentUserMessage !== undefined ? event.currentUserMessage : event.prompt;
      if (typeof task !== "string" || !task.trim()) return;
      const key = keyOf(ctx);
      const userId = userOf(ctx as AnyRecord);
      const state: TurnState = { startedAt: Date.now(), task, userId, skillsLoaded: [] };
      if (turns.size > MAX_TURNS_TRACKED) turns.delete(turns.keys().next().value!);
      turns.set(key, state);
      try {
        const r = await client.recall({ agent_id: agentId, user_id: userId ?? null }, task, key);
        state.recallId = r.recall_id;
        if (!r.instructions) return;
        return { prependContext: `<learned-context>\n${r.instructions}\n</learned-context>` };
      } catch (err) {
        log.warn?.(`[myelinate] recall failed, continuing without learned context: ${err}`);
      }
    });

    api.on("agent_end", async (event, ctx) => {
      const key = keyOf(ctx);
      const state = turns.get(key);
      turns.delete(key);
      if (!state) return;
      const messages = (Array.isArray(event.messages) ? event.messages : []) as AnyRecord[];
      const outcome: Outcome = event.success === false || event.error ? "failure" : "unknown";
      const ep: EpisodeIn = {
        scope: { agent_id: agentId, user_id: state.userId ?? null },
        session_id: key,
        recall_id: state.recallId ?? null,
        task: state.task,
        messages: messages
          .filter((m) => m?.role === "user" || m?.role === "assistant")
          .map((m) => ({ role: m.role, content: textOf(m.content) }))
          .filter((m) => m.content)
          .slice(-40),
        tool_calls: toolCallsOf(messages),
        skills_loaded: state.skillsLoaded,
        outcome,
        error: event.error ? String(event.error) : null,
        duration_ms: event.durationMs ?? Date.now() - state.startedAt,
        framework: "openclaw",
        metadata: { agentId: ctx.agentId, runId: event.runId ?? ctx.runId },
      };
      try {
        const out = await client.record(ep);
        lastEpisode.set(key, out.episode_id);
      } catch (err) {
        log.warn?.(`[myelinate] failed to record episode: ${err}`);
      }
    });

    api.registerTool((toolCtx: OpenClawPluginToolContext) => ({
      name: "skill_view",
      label: "Learned skill",
      description: "Load the full instructions of a learned skill by name (listed under 'Other learned skills').",
      parameters: Type.Object({ name: Type.String() }),
      async execute(_id: string, params: { name: string }) {
        const md = await client.skillMarkdown(agentId, params.name);
        if (md) turns.get(keyOf(toolCtx))?.skillsLoaded.push(params.name); // credit the skill with this turn's outcome
        return { content: [{ type: "text" as const, text: md ?? `No skill named '${params.name}'.` }], details: {} };
      },
    }), { name: "skill_view" });

    api.registerTool((toolCtx: OpenClawPluginToolContext) => ({
      name: "remember",
      label: "Remember",
      description:
        "Save one short durable memory. kind: 'fact' (environment/project), 'preference' (about the user), " +
        "or 'lesson' (a mistake to avoid). Never store secrets.",
      parameters: Type.Object({
        content: Type.String(),
        kind: Type.Optional(Type.Union([Type.Literal("fact"), Type.Literal("preference"), Type.Literal("lesson")])),
      }),
      async execute(_id: string, params: { content: string; kind?: "fact" | "preference" | "lesson" }) {
        const userId = cfg.perUserMemory === false ? null : (toolCtx.requesterSenderId ?? null);
        await client.remember({ agent_id: agentId, user_id: userId }, params.content, params.kind ?? "fact");
        return { content: [{ type: "text" as const, text: "Saved." }], details: {} };
      },
    }), { name: "remember" });

    api.registerTool((toolCtx: OpenClawPluginToolContext) => ({
      name: "rate_last_turn",
      label: "Rate last turn",
      description:
        "Record the user's verdict on your previous answer when they clearly approve or complain. " +
        "score 0.0 (wrong) to 1.0 (exactly right).",
      parameters: Type.Object({
        score: Type.Number({ minimum: 0, maximum: 1 }),
        comment: Type.Optional(Type.String()),
        correction: Type.Optional(Type.String({ description: "What the right answer or approach was, if the user said" })),
      }),
      async execute(_id: string, params: { score: number; comment?: string; correction?: string }) {
        // In-process map first; the gateway lookup covers CLI runs and multiple OpenClaw replicas.
        const episodeId = lastEpisode.get(keyOf(toolCtx)) ?? (await client.latestEpisode(agentId, keyOf(toolCtx)));
        if (!episodeId) return { content: [{ type: "text" as const, text: "No previous turn to rate." }], details: {} };
        await client.feedback(episodeId, params.score, params.comment, params.correction);
        return { content: [{ type: "text" as const, text: "Feedback recorded." }], details: {} };
      },
    }), { name: "rate_last_turn" });
  },
});
