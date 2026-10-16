"""LLM used internally for learning (not the host agent's own model)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

import anthropic
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
