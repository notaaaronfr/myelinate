"""LLM used internally for learning (not the host agent's own model)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

import anthropic
import httpx
from anthropic.types.beta import BetaOutputConfigParam

UsageSink = Callable[[int, int], None]


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    async def complete_json(self, system: str, prompt: str, schema: dict[str, Any],
                            usage: UsageSink | None = None) -> dict[str, Any]: ...


class AnthropicLLM:
    def __init__(self, model: str, effort: str, max_tokens: int, client: anthropic.AsyncAnthropic | None = None):
        self.model, self.effort, self.max_tokens = model, effort, max_tokens
        self.client = client or anthropic.AsyncAnthropic()

    async def complete_json(self, system: str, prompt: str, schema: dict[str, Any],
                            usage: UsageSink | None = None) -> dict[str, Any]:
        output_config: BetaOutputConfigParam = {
            "effort": self.effort,  # type: ignore[typeddict-item]
            "format": {"type": "json_schema", "schema": schema},
        }
        try:
            resp = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,  # stable per prompt type, so the prefix caches across calls
                messages=[{"role": "user", "content": prompt}],
                output_config=output_config,
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as e:
            raise LLMError(f"rate limited: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"api error {e.status_code}: {e}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"connection error: {e}") from e
        except anthropic.AnthropicError as e:
            raise LLMError(f"anthropic error: {e}") from e
        except TypeError as e:  # raised by the SDK when no API key / auth method can be resolved
            raise LLMError(f"no Anthropic credentials configured ({e})") from e
        if usage and resp.usage:  # all input the request consumed: uncached + cache writes + cache reads
            u = resp.usage
            usage(u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0),
                  u.output_tokens)
        if resp.stop_reason == "refusal":
            raise LLMError("model declined the request")
        if resp.stop_reason == "max_tokens":
            raise LLMError("output truncated at max_tokens")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise LLMError("no text block in response")
        return json.loads(text)


class OpenAICompatibleLLM:
    """Any OpenAI-style /chat/completions endpoint: OpenRouter, Azure OpenAI, vLLM, LiteLLM, Ollama.

    `extra_body` is merged into every request (for example OpenRouter's
    `{"provider": {"data_collection": "deny"}}`), so routing and privacy policy stay in configuration."""

    def __init__(self, base_url: str, api_key: str | None, model: str, max_tokens: int,
                 extra_body: dict[str, Any] | None = None, json_mode: str = "json_schema",
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 600.0) -> None:
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self.model, self.max_tokens, self.extra, self.json_mode = model, max_tokens, extra_body or {}, json_mode
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/", headers=headers, timeout=timeout,
                                      transport=transport)

    async def complete_json(self, system: str, prompt: str, schema: dict[str, Any],
                            usage: UsageSink | None = None) -> dict[str, Any]:
        if self.json_mode == "json_schema":
            fmt: dict[str, Any] = {"type": "json_schema", "json_schema": {"name": "result", "strict": True,
                                                                           "schema": schema}}
        else:
            fmt = {"type": "json_object"}
            prompt += "\n\nReply with one JSON object that matches this JSON schema:\n" + json.dumps(schema)
        body = {"model": self.model, "max_tokens": self.max_tokens, "response_format": fmt,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                **self.extra}
        try:
            r = await self.http.post("chat/completions", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"connection error: {e}") from e
        if r.status_code == 429:
            raise LLMError("rate limited")
        if r.status_code >= 400:
            raise LLMError(f"api error {r.status_code}: {r.text[:300]}")
        data = r.json()
        if usage and data.get("usage"):
            usage(int(data["usage"].get("prompt_tokens") or 0), int(data["usage"].get("completion_tokens") or 0))
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise LLMError("output truncated at max_tokens")
        content = (choice.get("message") or {}).get("content")
        if not content:
            raise LLMError(f"no content in response (finish_reason={choice.get('finish_reason')})")
        text = content.strip()
        if text.startswith("```"):  # some models fence JSON even in JSON mode
            text = text.strip("`").removeprefix("json").strip()
        try:
            return json.loads(text)
        except ValueError as e:
            raise LLMError(f"model returned invalid JSON: {e}") from e


class ScriptedLLM:
    """Deterministic stand-in for tests and air-gapped installs: responses come from a callable."""

    def __init__(self, fn: Callable[[str, str, dict[str, Any]], dict[str, Any]]):
        self.fn = fn
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system: str, prompt: str, schema: dict[str, Any],
                            usage: UsageSink | None = None) -> dict[str, Any]:
        self.calls.append((system, prompt))
        if usage:
            usage(len(prompt) // 4, 100)
        return self.fn(system, prompt, schema)


class DisabledLLM:
    async def complete_json(self, system: str, prompt: str, schema: dict[str, Any],
                            usage: UsageSink | None = None) -> dict[str, Any]:
        raise LLMError("no LLM configured (MYELINATE_LLM_PROVIDER=none)")
