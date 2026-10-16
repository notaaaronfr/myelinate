// Minimal, dependency-free client for the Myelinate gateway REST API.
// Reusable from any TypeScript agent framework (OpenClaw, Mastra, LangGraph.js, VoltAgent, ...).

export type Outcome = "success" | "failure" | "partial" | "unknown";

export interface Scope {
  agent_id: string;
  user_id?: string | null;
}

export interface RecallResponse {
  recall_id: string;
  instructions: string;
  skills: { id: string; name: string; description: string; version: number; score: number }[];
  memories: { id: string; kind: string; content: string; score: number }[];
}

export interface EpisodeIn {
  scope: Scope;
  idempotency_key?: string;
  session_id?: string | null;
  recall_id?: string | null;
  task?: string | null;
  messages: { role: string; content: string }[];
  tool_calls?: { name: string; arguments?: unknown; result?: string | null; error?: string | null }[];
  outcome: Outcome;
  verification?: { passed: boolean; details?: string | null; source?: string } | null;
  skills_loaded?: string[];
  error?: string | null;
  duration_ms?: number;
  framework?: string;
  metadata?: Record<string, unknown>;
}

export class GatewayClient {
  private readonly baseUrl: string;
  private readonly apiKey?: string;
  private readonly timeoutMs: number;

  constructor(baseUrl: string, apiKey?: string, timeoutMs = 4000) {
    this.baseUrl = baseUrl;
    this.apiKey = apiKey;
    this.timeoutMs = timeoutMs;
  }

  private async req<T>(method: string, path: string, body?: unknown, retries = 2): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      try {
        const res = await fetch(this.baseUrl.replace(/\/+$/, "") + path, { // keeps any path prefix in baseUrl
          method,
          headers: {
            "content-type": "application/json",
            ...(this.apiKey ? { authorization: `Bearer ${this.apiKey}` } : {}),
          },
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: AbortSignal.timeout(this.timeoutMs),
        });
        if (res.ok) {
          const text = await res.text();
          return (text && res.headers.get("content-type")?.includes("json") ? JSON.parse(text) : text) as T;
        }
        if ((res.status < 500 && res.status !== 429) || attempt >= retries) {
          throw new Error(`myelinate ${res.status}: ${await res.text()}`);
        }
      } catch (err) {
        if (attempt >= retries || (err instanceof Error && err.message.startsWith("myelinate 4"))) throw err;
      }
      await new Promise((r) => setTimeout(r, 200 * 2 ** attempt));
    }
  }

  recall(scope: Scope, query: string, sessionId?: string): Promise<RecallResponse> {
    return this.req("POST", "/v1/recall", { scope, query, session_id: sessionId ?? null });
  }

  record(ep: EpisodeIn): Promise<{ episode_id: string; reflection_scheduled: boolean }> {
    // Same key on every retry of this call, so a timeout-then-retry can't record the episode twice.
    return this.req("POST", "/v1/episodes", { idempotency_key: `ts:${crypto.randomUUID()}`, ...ep });
  }

  feedback(episodeId: string, score: number, comment?: string, correction?: string): Promise<void> {
    return this.req("POST", "/v1/feedback", {
      episode_id: episodeId, score, comment: comment ?? null, correction: correction ?? null,
    });
  }

  async latestEpisode(agentId: string, sessionId: string): Promise<string | null> {
    try {
      const r = await this.req<{ episode_id: string }>(
        "GET", `/v1/episodes/latest?agent_id=${encodeURIComponent(agentId)}&session_id=${encodeURIComponent(sessionId)}`);
      return r.episode_id;
    } catch (err) {
      if (err instanceof Error && err.message.startsWith("myelinate 404")) return null;
      throw err;
    }
  }

  ask(scope: Scope, question: string): Promise<{ answer: string; evidence: string[] }> {
    return this.req("POST", "/v1/ask", { scope, question });
  }

  remember(scope: Scope, content: string, kind: "fact" | "preference" | "lesson" = "fact"): Promise<unknown> {
    return this.req("POST", "/v1/memories", { scope, kind, content });
  }

  async skillMarkdown(agentId: string, name: string): Promise<string | null> {
    try {
      return await this.req<string>(
        "GET",
        `/v1/skills/${encodeURIComponent(name)}/SKILL.md?agent_id=${encodeURIComponent(agentId)}`,
      );
    } catch (err) {
      if (err instanceof Error && err.message.startsWith("myelinate 404")) return null;
      throw err;
    }
  }
}
