"""Exercises the real Anthropic SDK request path with a mocked HTTP transport (no network, no key)."""

from __future__ import annotations

import json

import anthropic
import httpx2 as httpx
import pytest

from myelinate import prompts as P
from myelinate.llm import AnthropicLLM, LLMError



def client(handler) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key="test", max_retries=0,
                                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def reply(text: str, stop: str = "end_turn") -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 900,
                      "cache_creation_input_tokens": 0}}


async def test_request_shape_and_parsing():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = str(req.url), req.headers, json.loads(req.content)
        return httpx.Response(200, json=reply(json.dumps({"reason": "ok", "outcome": "success", "confidence": 0.9})))

    usage = []
    llm = AnthropicLLM("claude-opus-5-5", "medium", 16000, client=client(handler))
    out = await llm.complete_json(P.OUTCOME_SYSTEM, "run record", P.OUTCOME_SCHEMA, usage=lambda i, o: usage.append((i, o)))

    assert out == {"reason": "ok", "outcome": "success", "confidence": 0.9}
    b = seen["body"]
    assert seen["url"].endswith("/v1/messages?beta=true")
    assert b["model"] == "claude-opus-5-5" and b["max_tokens"] == 16000 and b["system"] == P.OUTCOME_SYSTEM
    assert b["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": P.OUTCOME_SCHEMA}}
    assert b["fallbacks"] == "default" and b["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in b and "temperature" not in b  # rejected on Opus 5.5
    assert "server-side-fallback-2026-07-01" in seen["headers"]["anthropic-beta"]
    assert usage == [(2100, 80)]


@pytest.mark.parametrize(("status", "body", "match"), [
    (200, reply("", "refusal"), "declined"),
    (200, reply('{"a":', "max_tokens"), "truncated"),
    (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, "rate limited"),
    (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}, "api error 400"),
])
async def test_errors_become_llm_errors(status, body, match):
    llm = AnthropicLLM("claude-opus-5-5", "medium", 16000,
                       client=client(lambda req: httpx.Response(status, json=body)))
    with pytest.raises(LLMError, match=match):
        await llm.complete_json(P.JUDGE_SYSTEM, "x", P.JUDGE_SCHEMA)


def _check_strict(schema: dict, path: str = "$") -> None:
    """Structured-output constraints: every object closed with all properties required."""
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, path
        assert set(schema["required"]) == set(schema["properties"]), path
        for k, v in schema["properties"].items():
            _check_strict(v, f"{path}.{k}")
    if schema.get("type") == "array":
        _check_strict(schema["items"], f"{path}[]")


@pytest.mark.parametrize("name", [n for n in dir(P) if n.endswith("_SCHEMA")])
def test_all_schemas_are_strict(name):
    _check_strict(getattr(P, name))


async def test_openai_compatible_request_shape_and_privacy_passthrough():
    import httpx as hx

    from myelinate.llm import OpenAICompatibleLLM

    seen = {}

    def handler(req: hx.Request) -> hx.Response:
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers.get("authorization"), json.loads(req.content)
        return hx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '```json\n{"reason": "ok", "outcome": "failure", "confidence": 0.8}\n```'}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 40}})

    llm = OpenAICompatibleLLM("https://openrouter.ai/api/v1/", "or-key", "anthropic/claude-opus-5.5", 4000,
                              extra_body={"provider": {"data_collection": "deny", "zdr": True}},
                              transport=hx.MockTransport(handler))
    usage = []
    out = await llm.complete_json(P.OUTCOME_SYSTEM, "run", P.OUTCOME_SCHEMA, usage=lambda i, o: usage.append((i, o)))
    assert out["outcome"] == "failure" and usage == [(900, 40)]
    b = seen["body"]
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions" and seen["auth"] == "Bearer or-key"
    assert b["provider"] == {"data_collection": "deny", "zdr": True} and b["model"] == "anthropic/claude-opus-5.5"
    assert b["response_format"]["json_schema"]["schema"] == P.OUTCOME_SCHEMA and b["messages"][0]["role"] == "system"


async def test_openai_compatible_errors():
    import httpx as hx

    from myelinate.llm import OpenAICompatibleLLM

    for status, body, match in [(429, {}, "rate limited"), (500, {"error": "x"}, "api error 500"),
                                (200, {"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}, "truncated"),
                                (200, {"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]}, "invalid JSON")]:
        llm = OpenAICompatibleLLM("http://x/v1", None, "m", 100,
                                  transport=hx.MockTransport(lambda req, s=status, b=body: hx.Response(s, json=b)))
        with pytest.raises(LLMError, match=match):
            await llm.complete_json("s", "p", {"type": "object"})
