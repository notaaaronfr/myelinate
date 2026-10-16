"""Regression tests for issues found in code review."""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
from test_gateway import ADMIN, drain, episode, learn_deploy_skill, make, op, recall, skill

from myelinate.models import EpisodeIn, Outcome, RecallRequest, Scope

pytest_plugins = ["test_gateway"]  # shares the `env` fixture


def test_import_preserves_step_order(env):
    c, brain, app, admin, agent = env
    md = "---\nname: ordered\ndescription: Steps must stay in order\n---\n## Steps\n" + "".join(
        f"{i}. Step number {w} of the procedure here.\n" for i, w in enumerate(["one", "two", "three", "four", "five"], 1))
    c.post("/v1/skills/import", json={"agent_id": "ops", "markdown": md}, headers=admin)
    out = c.get("/v1/skills/ordered/SKILL.md", params={"agent_id": "ops"}, headers=agent).text
    order = [w for w in ["one", "two", "three", "four", "five"] if f"Step number {w}" in out]
    assert [out.index(f"Step number {w}") for w in order] == sorted(out.index(f"Step number {w}") for w in order)


def test_idempotent_record_counts_once(env):
    c, brain, app, admin, agent = env
    learn_deploy_skill(c, brain, app, agent)
    r = recall(c, agent, session_id="idem")
    body = episode("success", r["recall_id"], tool_calls=[], idempotency_key="client-retry-1")
    a = c.post("/v1/episodes", json=body, headers=agent).json()
    b = c.post("/v1/episodes", json=body, headers=agent).json()
    assert a["episode_id"] == b["episode_id"]
    assert skill(c, agent)["uses"] == 1


def test_concurrent_records_lose_no_credit(env):
    c, brain, app, admin, agent = env
    learn_deploy_skill(c, brain, app, agent)
    eng = app.state.engine
    rids = [recall(c, agent, session_id=f"cc{i}")["recall_id"] for i in range(12)]

    async def burst():
        await asyncio.gather(*(eng.record("acme", EpisodeIn(scope=Scope(agent_id="ops"), recall_id=rid, task="deploy",
                                                            outcome=Outcome.success)) for rid in rids))

    c.portal.call(burst)
    sk = skill(c, agent)
    assert sk["uses"] == 12
    assert abs(sk["success_rate"] - 13 / 14) < 1e-3  # Beta(1+12, 1+0); API rounds to 3 dp


def test_feedback_beats_late_judge(env):
    c, brain, app, admin, agent = env
    learn_deploy_skill(c, brain, app, agent)
    r = recall(c, agent, session_id="fb")
    ep = c.post("/v1/episodes", json=episode("unknown", r["recall_id"], tool_calls=[]), headers=agent).json()
    c.post("/v1/feedback", json={"episode_id": ep["episode_id"], "score": 0.0}, headers=agent)
    eng = app.state.engine
    c.portal.call(lambda: eng.credit("acme", ep["episode_id"], source="judge", judged=("success", 0.99)))
    assert abs(skill(c, agent)["success_rate"] - 1 / 3) < 1e-3  # Beta(1, 2): feedback 0.0 stands


def test_queued_changesets_all_released_and_stale_fail(env):
    c, brain, app, admin, agent = env
    learn_deploy_skill(c, brain, app, agent)
    for i in range(6):
        rr = recall(c, agent, session_id=f"w{i}")
        c.post("/v1/episodes", json=episode("partial", rr["recall_id"], tool_calls=[]), headers=agent)
    drain(c, app)
    bids = [b["id"] for b in skill(c, agent)["bullets"]]
    gov = app.state.engine.gov

    async def propose(i: int):
        return await gov.propose("acme", "t", "ops", [op("update", bullet_id=bids[i], content=f"Updated rule {i}: always "
                                                         f"check the namespace pull secret before applying.")],
                                 "t", "api", auto=False)

    first, second, third = (c.portal.call(propose, i) for i in range(3))
    assert c.post(f"/v1/changesets/{first.id}/review", json={"approve": True}, headers=admin).json()["status"] == "canary"
    for cs in (second, third):
        assert c.post(f"/v1/changesets/{cs.id}/review", json={"approve": True}, headers=admin).json()["status"] == "approved"
    for i in range(200):  # candidate wins -> promoted -> queued ones re-evaluated
        rr = recall(c, agent, session_id=f"q{i}")
        if rr["skills"][0]["arm"] is None:
            break
        c.post("/v1/episodes", json=episode("success" if rr["skills"][0]["arm"] == "candidate" else "failure",
                                            rr["recall_id"], tool_calls=[]), headers=agent)
    statuses = {x["id"]: x["status"] for x in c.get("/v1/changesets", params={"status": ""}, headers=admin).json()}
    assert statuses[first.id] == "applied"
    # Proposed against the pre-promotion version: they fail as stale instead of silently rebasing.
    assert statuses[second.id] == statuses[third.id] == "failed"


def test_otel_malformed_and_retry(env):
    c, brain, app, admin, agent = env

    def attr(k, v):
        return {"key": k, "value": {"stringValue": v}}

    good = {"traceId": "tr-ok", "spanId": "1", "name": "invoke_agent", "status": None,
            "startTimeUnixNano": "abc",
            "attributes": [attr("gen_ai.operation.name", "invoke_agent"),
                           attr("myelinate.verification.passed", "false"),
                           {"key": "myelinate.outcome", "value": {"arrayValue": {"values": []}}},
                           attr("gen_ai.input.messages", json.dumps([{"role": "user", "content": "hi"}]))]}
    junk = [{"traceId": "tr-bad", "attributes": "nope"}, "not-a-span", {"traceId": "tr-x", "attributes": [{"value": 1}]}]
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [good, *junk]}]}]}
    r1 = c.post("/v1/traces", json=payload, headers=agent)
    r2 = c.post("/v1/traces", json=payload, headers=agent)  # exporter retry
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["episode_ids"] == r2.json()["episode_ids"] and len(r1.json()["episode_ids"]) == 1
    assert c.post("/v1/traces", json=["not", "an", "object"], headers=agent).status_code == 200

    async def load():
        from myelinate.db import EpisodeRow
        async with app.state.db.session() as ses:
            return await ses.get(EpisodeRow, r1.json()["episode_ids"][0])

    assert c.portal.call(load).meta["verification"]["passed"] is False  # "false" is not truthy


def test_chunked_body_limit(db_url):
    app, _ = make(db_url, max_request_bytes=1000)
    with TestClient(app) as c:
        h = {"authorization": f"Bearer {ADMIN}", "x-tenant-id": "acme", "content-type": "application/json"}

        def gen():
            yield b'{"query": "'
            yield b"x" * 5000
            yield b'"}'

        assert c.post("/v1/recall", content=gen(), headers=h).status_code == 413
        assert c.post("/v1/recall", content=b"{}", headers=h | {"content-length": "abc"}).status_code in (400, 413)


def test_erasure_removes_recalls_and_queries_are_redacted(env):
    c, brain, app, admin, agent = env
    c.post("/v1/recall", json={"scope": {"agent_id": "ops", "user_id": "u9"},
                               "query": "email bob@example.com about deploy"}, headers=agent)

    async def recalls():
        from sqlalchemy import select

        from myelinate.db import RecallRow
        async with app.state.db.session() as ses:
            return (await ses.scalars(select(RecallRow).where(RecallRow.user_id == "u9"))).all()

    rows = c.portal.call(recalls)
    assert rows and "bob@example.com" not in rows[0].query
    c.delete("/v1/admin/users/u9", headers=admin)
    assert c.portal.call(recalls) == []


def test_learned_facts_stay_with_the_user(env):
    c, brain, app, admin, agent = env
    brain.memories = [{"kind": "fact", "content": "Their staging cluster is called blue-fox"}]
    c.post("/v1/episodes", json=episode(), headers=agent)  # user u1
    drain(c, app)
    eng = app.state.engine
    r_other = c.portal.call(lambda: eng.recall("acme", RecallRequest(scope=Scope(agent_id="ops", user_id="u2"),
                                                                     query="staging cluster blue-fox name")))
    r_owner = c.portal.call(lambda: eng.recall("acme", RecallRequest(scope=Scope(agent_id="ops", user_id="u1"),
                                                                     query="staging cluster blue-fox name")))
    assert not r_other.memories and r_owner.memories


def test_duplicate_keep_decisions_dont_abort_curation(env):
    c, brain, app, admin, agent = env
    c.post("/v1/skills", json={"agent_id": "ops", "name": "rotate-cert", "description": "Rotate TLS certificates",
                               "bullets": [{"section": "steps", "content": "Renew the certificate with cert-manager before it expires in production"},
                                           {"section": "steps", "content": "Renew the certificate with cert-manager before it expires in prod"}]},
           headers=admin)
    a, b = sorted(x["id"] for x in skill(c, agent, "rotate-cert")["bullets"])
    brain.dedup = [{"a": a, "b": b, "action": "keep", "merged_content": ""}] * 2
    c.post("/v1/agents/ops/curate", headers=admin)
    drain(c, app)

    async def jobs():
        from sqlalchemy import select

        from myelinate.db import JobRow
        async with app.state.db.session() as ses:
            return [(j.kind, j.status, j.last_error) for j in (await ses.scalars(select(JobRow))).all()]

    assert ("curate", "done", None) in c.portal.call(jobs)


def test_evolution_proposed_against_old_version_is_stale(env):
    c, brain, app, admin, agent = env
    learn_deploy_skill(c, brain, app, agent)
    sk = skill(c, agent)
    gov = app.state.engine.gov

    async def go():
        # ops derived from v1, but the skill moved to v2 before the proposal landed
        await gov.propose("acme", "t", "ops", [op("add", skill="deploy-k8s-service", section="notes",
                                                  content="Another rule that was written in the meantime by someone.")],
                          "t", "api")
        return await gov.propose("acme", "t", "ops", [op("add", skill="deploy-k8s-service", section="notes",
                                                         content="Rule derived from the old version of this skill.")],
                                 "t", "evolution", read_versions={sk["id"]: 1})

    cs = c.portal.call(go)
    st = {x["id"]: x["status"] for x in c.get("/v1/changesets", params={"status": ""}, headers=admin).json()}
    assert st[cs.id] == "failed" and skill(c, agent)["version"] == 2


def test_latest_episode_lookup_for_stateless_feedback(env):
    c, brain, app, admin, agent = env
    c.post("/v1/episodes", json=episode("unknown", tool_calls=[], session_id="sess-z"), headers=agent)
    last = c.post("/v1/episodes", json=episode("unknown", tool_calls=[], session_id="sess-z", task="second"),
                  headers=agent).json()["episode_id"]
    r = c.get("/v1/episodes/latest", params={"agent_id": "ops", "session_id": "sess-z"}, headers=agent)
    assert r.json()["episode_id"] == last
    assert c.get("/v1/episodes/latest", params={"agent_id": "ops", "session_id": "nope"}, headers=agent).status_code == 404
    other = {"authorization": f"Bearer {ADMIN}", "x-tenant-id": "globex"}
    assert c.get("/v1/episodes/latest", params={"agent_id": "ops", "session_id": "sess-z"}, headers=other).status_code == 404


def test_recall_injects_steps_in_authored_order(env):
    c, brain, app, admin, agent = env
    words = ["one", "two", "three", "four", "five", "six"]
    md = "---\nname: ordered-recall\ndescription: Deploy the ordered recall service steps\n---\n## Steps\n" + "".join(
        f"{i}. Step number {w} of the deploy procedure.\n" for i, w in enumerate(words, 1))
    c.post("/v1/skills/import", json={"agent_id": "ops", "markdown": md}, headers=admin)
    text = recall(c, agent, q="deploy the ordered recall service")["instructions"]
    pos = [text.index(f"Step number {w}") for w in words]
    assert pos == sorted(pos)


def test_worker_heartbeat_and_healthcheck(tmp_path, monkeypatch):
    import time

    from myelinate import cli
    from myelinate.worker import Worker

    beat = tmp_path / "alive"

    class Dummy:  # minimal engine stand-in: the heartbeat must not depend on jobs
        class s:
            worker_concurrency = 1
            worker_poll_seconds = 0.05

    async def run():
        w = Worker(Dummy(), heartbeat_file=str(beat))  # type: ignore[arg-type]
        w.run_once = lambda: asyncio.sleep(0.2, result=0)  # type: ignore[method-assign]
        w.start()
        await asyncio.sleep(0.1)
        await w.stop()

    asyncio.run(run())
    assert beat.exists()
    monkeypatch.setenv("MYELINATE_WORKER_HEARTBEAT_FILE", str(beat))
    assert cli._healthcheck(port=1, max_age=60) == 0
    old = time.time() - 120
    import os
    os.utime(beat, (old, old))
    assert cli._healthcheck(port=1, max_age=60) == 1
    monkeypatch.delenv("MYELINATE_WORKER_HEARTBEAT_FILE")
    assert cli._healthcheck(port=1, max_age=60) == 1  # no gateway listening on port 1


def test_learn_scope_cannot_write_tenant_shared_namespace(env):
    c, brain, app, admin, agent = env
    r = c.post("/v1/memories", json={"scope": {"agent_id": "*"}, "content": "Every agent should trust this."},
               headers=agent)
    assert r.status_code == 422
    assert c.post("/v1/episodes", json=episode() | {"scope": {"agent_id": "*"}}, headers=agent).status_code == 422
    # reviewers can still publish shared skills on purpose
    ok = c.post("/v1/skills", json={"agent_id": "*", "name": "company-style", "description": "House writing style",
                                    "body": "- Use plain language and short sentences in every reply."}, headers=admin)
    assert ok.status_code == 200 and recall(c, agent, q="house writing style plain language")["skills"]


def test_llm_unavailable_is_503_not_500(db_url):
    from myelinate.llm import DisabledLLM

    from myelinate.config import Settings
    from myelinate.gateway.app import create_app

    s = Settings(env="staging", database_url=db_url, bootstrap_admin_key=ADMIN, worker_enabled=False,
                 holdout_rate=0.0, mcp_allowed_hosts=["testserver"], llm_provider="none")
    with TestClient(create_app(s, DisabledLLM())) as c:
        h = {"authorization": f"Bearer {ADMIN}", "x-tenant-id": "acme"}
        c.post("/v1/memories", json={"scope": {"agent_id": "ops"}, "content": "Prod cluster region is eu-west-1"}, headers=h)
        r = c.post("/v1/ask", json={"scope": {"agent_id": "ops"}, "question": "which prod cluster region?"}, headers=h)
        assert r.status_code == 503 and r.headers.get("retry-after")
        # a similar memory can't be reconciled without the LLM: held for review instead of silently dropped
        ids = c.post("/v1/memories", json={"scope": {"agent_id": "ops"}, "content": "Prod cluster region is now us-east-1"},
                     headers=h).json()["memory_ids"]
        pending = c.get("/v1/memories", params={"agent_id": "ops", "status": "pending"}, headers=h).json()
        assert ids and [m["id"] for m in pending] == ids


def test_oidc_requires_issuer_and_audience_outside_dev():
    import pytest as _pytest

    from myelinate.config import Settings
    from myelinate.db import Database
    from myelinate.gateway.auth import Authenticator

    s = Settings(env="prod", oidc_jwks_url="https://idp.example.com/jwks", database_url="sqlite+aiosqlite:///:memory:")
    with _pytest.raises(RuntimeError, match="OIDC requires"):
        Authenticator(s, Database(s.database_url))


def test_purge_covers_recalls_jobs_and_audit(env):
    c, brain, app, admin, agent = env
    recall(c, agent)
    out = c.portal.call(app.state.engine.purge_expired)
    assert set(out) == {"episodes", "recalls", "jobs", "audit", "otel_pending"}


def _proto_trace(trace_hex: str, spans: list[dict]) -> bytes:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    req = ExportTraceServiceRequest()
    ss = req.resource_spans.add().scope_spans.add()
    for i, sp in enumerate(spans):
        s = ss.spans.add()
        s.trace_id, s.span_id, s.name = bytes.fromhex(trace_hex), bytes([i + 1] * 8), sp["name"]
        s.start_time_unix_nano = sp.get("start", i + 1)
        if sp.get("error"):
            s.status.code, s.status.message = 2, sp["error"]
        for k, v in sp["attrs"].items():
            a = s.attributes.add()
            a.key = k
            a.value.string_value = v
    return req.SerializeToString()


def test_otlp_protobuf_gzip_and_split_trace_merge(env):
    import gzip

    c, brain, app, admin, agent = env
    learn_deploy_skill(c, brain, app, agent)
    trace = "0af7651916cd43dd8448eb211c80319c"
    h = agent | {"content-type": "application/x-protobuf", "content-encoding": "gzip", "x-agent-id": "ops"}
    # export 1: child spans only (tool error + skill load), as SDKs usually flush children first
    part1 = _proto_trace(trace, [
        {"name": "chat", "attrs": {"gen_ai.operation.name": "chat", "gen_ai.input.messages":
                                   '[{"role":"user","parts":[{"type":"text","content":"deploy billing"}]}]'}},
        {"name": "execute_tool kubectl", "error": "ImagePullBackOff",
         "attrs": {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "kubectl"}},
        {"name": "execute_tool load_skill", "attrs": {"gen_ai.operation.name": "execute_tool",
                                                      "gen_ai.skill.name": "deploy-k8s-service"}}])
    r1 = c.post("/v1/traces", content=gzip.compress(part1), headers=h)
    assert r1.status_code == 200, r1.text
    # export 2: the root agent span with the verified outcome arrives later
    part2 = _proto_trace(trace, [{"name": "invoke_agent ops", "attrs": {
        "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "someone-else",
        "myelinate.verification.passed": "false"}}])
    r2 = c.post("/v1/traces", content=gzip.compress(part2), headers=h)
    assert r1.json()["episode_ids"] == r2.json()["episode_ids"]  # merged into one episode

    async def load_ep():
        from myelinate.db import EpisodeRow
        async with app.state.db.session() as ses:
            return await ses.get(EpisodeRow, r1.json()["episode_ids"][0])

    ep = c.portal.call(load_ep)
    assert ep.agent_id == "ops"  # X-Agent-Id beats gen_ai.agent.name
    assert ep.meta["verification"]["passed"] is False and ep.tool_calls[0]["error"] == "ImagePullBackOff"
    sk = skill(c, agent)
    assert sk["uses"] == 1 and sk["success_rate"] < 0.5  # credited once, with the late verification
    assert c.post("/v1/traces", content=b"\x00garbage", headers=agent | {"content-type": "application/x-protobuf"}
                  ).status_code == 400
    assert c.post("/v1/traces", content=b"{nope", headers=agent | {"content-type": "application/json"}).status_code == 400
    assert c.post("/v1/traces", content=b"x", headers=agent | {"content-type": "text/plain"}).status_code == 415
    assert c.post("/v1/traces", json={}, headers=agent | {"x-agent-id": "bad agent!"}).status_code == 400


def test_mcp_requires_auth_up_front(env):
    c, brain, app, admin, agent = env
    h = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=h)
    assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"


def test_otlp_child_spans_before_root_and_late_output_are_kept(env):
    c, brain, app, admin, agent = env
    trace = "1af7651916cd43dd8448eb211c80319d"
    h = agent | {"content-type": "application/x-protobuf", "x-agent-id": "ops"}
    tools_only = _proto_trace(trace, [{"name": "execute_tool kubectl", "error": "ImagePullBackOff",
                                       "attrs": {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "kubectl"}}])
    r0 = c.post("/v1/traces", content=tools_only, headers=h)
    assert r0.status_code == 200 and r0.json()["episode_ids"] == []  # held until the trace's root arrives
    chat = _proto_trace(trace, [{"name": "chat", "attrs": {"gen_ai.operation.name": "chat", "gen_ai.input.messages":
                                 '[{"role":"user","parts":[{"type":"text","content":"deploy billing"}]}]'}}])
    eid = c.post("/v1/traces", content=chat, headers=h).json()["episode_ids"][0]
    root = _proto_trace(trace, [{"name": "invoke_agent", "attrs": {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.output.messages": '[{"role":"assistant","parts":[{"type":"text","content":"rolled back"}]}]'}}])
    assert c.post("/v1/traces", content=root, headers=h).json()["episode_ids"] == [eid]

    async def load():
        from myelinate.db import EpisodeRow
        async with app.state.db.session() as ses:
            return await ses.get(EpisodeRow, eid)

    ep = c.portal.call(load)
    assert ep.tool_calls and ep.tool_calls[0]["error"] == "ImagePullBackOff"  # early child span kept
    assert any(m["content"] == "rolled back" for m in ep.messages)  # late output appended
