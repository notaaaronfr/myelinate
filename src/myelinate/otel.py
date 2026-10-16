"""Zero-code ingestion: OTLP/HTTP JSON traces -> episodes.

Frameworks that emit OpenTelemetry GenAI spans (Microsoft Agent Framework, Google ADK, Strands,
Pydantic AI, OpenClaw's diagnostics-otel, CrewAI/LangGraph via instrumentors) can feed the learning
loop by adding this gateway as an OTLP exporter endpoint: no adapter code at all.

Understood conventions:
- OTel GenAI semconv: gen_ai.operation.name (invoke_agent / chat / execute_tool), gen_ai.agent.name,
  gen_ai.conversation.id, gen_ai.input.messages / gen_ai.output.messages, gen_ai.tool.name,
  gen_ai.tool.call.arguments / .result, gen_ai.skill.name (skill loads -> credit assignment), error.type.
- OpenInference (Arize/Phoenix instrumentors): openinference.span.kind, input.value / output.value,
  tool.name, llm.input_messages.N.message.role/content.
- OpenClaw diagnostics: `tool.execution` spans.
- Correlation: a `myelinate.recall_id` attribute (set by our adapters) links the trace to its recall;
  `myelinate.outcome` / `myelinate.verification.passed` carry labels when the app has them.

Spans are grouped per trace (one turn); the conversation id becomes the session id. Only content the exporter chose to capture
(sensitive-data opt-in) is available; without it, episodes still carry tools, errors and outcome.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from .models import AGENT_ID, EpisodeIn, Message, Outcome, Scope, ToolCall, Verification


def _val(v: dict[str, Any]) -> Any:
    for k in ("stringValue", "boolValue", "doubleValue"):
        if k in v:
            return v[k]
    if "intValue" in v:
        return int(v["intValue"])
    if "arrayValue" in v:
        return [_val(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {kv["key"]: _val(kv["value"]) for kv in v["kvlistValue"].get("values", [])}
    return None


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    return {a["key"]: _val(a.get("value", {})) for a in span.get("attributes", [])}


def _messages(raw: Any) -> list[Message]:
    """gen_ai.*.messages: JSON [{role, parts:[{type:'text', content}]}] (or legacy {role, content})."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [Message(role="user", content=raw)]
    out = []
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict):
            continue
        parts = m.get("parts")
        if isinstance(parts, list):
            text = "\n".join(str(p.get("content", "")) for p in parts if isinstance(p, dict) and p.get("type") == "text")
        else:
            text = m.get("content") if isinstance(m.get("content"), str) else ""
        if text:
            out.append(Message(role=str(m.get("role", "user")), content=text))
    return out


def _openinference_messages(a: dict[str, Any], prefix: str) -> list[Message]:
    msgs: dict[int, dict[str, str]] = defaultdict(dict)
    for k, v in a.items():
        if k.startswith(prefix):
            rest = k[len(prefix):].split(".", 2)  # N.message.role
            if len(rest) == 3 and rest[0].isdigit():
                msgs[int(rest[0])][rest[2]] = str(v)
    return [Message(role=m.get("role", "user"), content=m["content"]) for _, m in sorted(msgs.items())
            if m.get("content")]


def _list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _bool(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"))


def _status(span: dict[str, Any]) -> dict[str, Any]:
    return _dict(span.get("status"))


def _failed(span: dict[str, Any]) -> bool:
    return _status(span).get("code") in (2, "2", "STATUS_CODE_ERROR")


def spans_to_episodes(otlp: Any, default_agent: str = "default") -> list[tuple[EpisodeIn, str | None]]:
    """Returns (episode, recall_id) pairs. Malformed traces are skipped, never fail the whole batch."""
    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for rs in _list(_dict(otlp).get("resourceSpans")):
        try:
            res = {a["key"]: _val(_dict(a.get("value"))) for a in _list(_dict(_dict(rs).get("resource")).get("attributes"))}
        except (KeyError, TypeError):
            res = {}
        for ss in _list(_dict(rs).get("scopeSpans")):
            for sp in _list(_dict(ss).get("spans")):
                if not isinstance(sp, dict):
                    continue
                try:
                    attrs = _attrs(sp)
                except (KeyError, TypeError, AttributeError):
                    continue
                # One trace = one turn; child spans rarely repeat gen_ai.conversation.id, so group by trace.
                groups[str(sp.get("traceId", ""))].append((sp, {**res, **attrs}))

    episodes = []
    for trace_id, spans in groups.items():
        try:
            ep = _episode(trace_id, spans, default_agent)
        except Exception:  # noqa: BLE001 - one bad trace must not drop the rest
            continue
        if ep is not None:
            episodes.append(ep)
    return episodes


def _episode(trace_id: str, spans: list[tuple[dict[str, Any], dict[str, Any]]],
             default_agent: str) -> tuple[EpisodeIn, str | None] | None:
    spans.sort(key=lambda x: _int(x[0].get("startTimeUnixNano")))
    op = lambda a: a.get("gen_ai.operation.name") or str(a.get("openinference.span.kind", "")).lower()  # noqa: E731
    agent_spans = [(s, a) for s, a in spans if op(a) in ("invoke_agent", "agent", "invoke_workflow")]
    chat = [(s, a) for s, a in spans if op(a) in ("chat", "generate_content", "text_completion", "llm")]
    if not agent_spans and not chat:
        return None
    root_s, root_a = agent_spans[0] if agent_spans else chat[0]
    conv = next((a["gen_ai.conversation.id"] for _, a in spans if a.get("gen_ai.conversation.id")), None)
    root_a = {"gen_ai.conversation.id": conv, **{k: v for k, v in root_a.items() if v is not None}}

    msgs = _messages(root_a.get("gen_ai.input.messages")) or (
        _messages(chat[0][1].get("gen_ai.input.messages")) if chat else [])
    if not msgs and root_a.get("input.value"):
        msgs = _openinference_messages(root_a, "llm.input_messages.") or [
            Message(role="user", content=str(root_a["input.value"]))]
    out_msgs = _messages(root_a.get("gen_ai.output.messages")) or (
        _messages(chat[-1][1].get("gen_ai.output.messages")) if chat else [])
    if not out_msgs and root_a.get("output.value"):
        out_msgs = [Message(role="assistant", content=str(root_a["output.value"]))]
    msgs = [m for m in msgs if m.role not in ("system",)] + out_msgs

    tools, skills = [], []
    for s, a in spans:
        if op(a) in ("execute_tool", "tool") or s.get("name", "").startswith("tool.execution"):
            if a.get("gen_ai.skill.name"):
                skills.append(str(a["gen_ai.skill.name"]))
            failed = (_failed(s) or a.get("error.type")
                      or (a.get("process.exit.code") not in (None, 0, "0")))
            tools.append(ToolCall(
                name=str(a.get("gen_ai.tool.name") or a.get("tool.name") or s.get("name", "tool"))[:200],
                arguments=_str(a.get("gen_ai.tool.call.arguments") or a.get("input.value")),
                result=None if failed else _str(a.get("gen_ai.tool.call.result") or a.get("output.value")),
                error=(_str(_status(s).get("message")) or str(a.get("error.type") or "error")) if failed else None))
        elif a.get("gen_ai.skill.name"):
            skills.append(str(a["gen_ai.skill.name"]))

    root_failed = _failed(root_s)
    label = root_a.get("myelinate.outcome")
    outcome = (Outcome(label) if isinstance(label, str) and label in Outcome.__members__
               else (Outcome.failure if root_failed else Outcome.unknown))
    ver = None
    if "myelinate.verification.passed" in root_a:
        ver = Verification(passed=_bool(root_a["myelinate.verification.passed"]), source="otel",
                           details=_str(root_a.get("myelinate.verification.details")))
    start, end = _int(root_s.get("startTimeUnixNano")), _int(root_s.get("endTimeUnixNano"))
    agent = str(root_a.get("myelinate.agent_id") or root_a.get("gen_ai.agent.name") or default_agent)
    if not re.match(AGENT_ID, agent):
        agent = default_agent
    ep = EpisodeIn(
        scope=Scope(agent_id=agent, user_id=_cap(root_a.get("myelinate.user_id") or root_a.get("user.id"), 256)),
        idempotency_key=f"otel:{trace_id}"[:256] if trace_id else None,  # exporter retries don't duplicate
        session_id=_cap(root_a.get("gen_ai.conversation.id") or root_a.get("session.id"), 256),
        task=next((m.content for m in msgs if m.role == "user"), None), messages=msgs, tool_calls=tools,
        outcome=outcome, verification=ver, error=_str(_status(root_s).get("message")) if root_failed else None,
        duration_ms=(end - start) // 1_000_000 if end > start else None,
        framework=_cap(root_a.get("gen_ai.provider.name") or root_a.get("telemetry.sdk.name") or "otel", 64),
        skills_loaded=list(dict.fromkeys(skills))[:50], metadata={"trace_id": trace_id})
    return ep, _cap(root_a.get("myelinate.recall_id"), 64)


def _cap(v: Any, n: int) -> str | None:
    s = _str(v)
    return s[:n] if s else None


def _str(v: Any) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v)
