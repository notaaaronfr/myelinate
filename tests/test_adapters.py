from __future__ import annotations

import pytest

from myelinate.client import EmbeddedClient
from myelinate.config import Settings
from myelinate.llm import ScriptedLLM
from myelinate.models import Outcome, SkillIn



@pytest.fixture
async def learner(db_url):
    s = Settings(database_url=db_url, reflect_every_n_episodes=100, holdout_rate=0)
    c = await EmbeddedClient("acme", s, ScriptedLLM(lambda *a: {"assessment": "", "actions": []})).start(run_worker=False)
    await c.engine.author_skill("acme", "test", SkillIn(
        agent_id="ops", name="rotate-tls-cert", description="Rotate an expiring TLS certificate",
        body="1. Issue a new cert with cert-manager.\n2. Roll the ingress pods."))
    yield c
    await c.aclose()


async def test_generic_adapter_injects_and_records(learner):
    from myelinate.adapters.generic import SelfLearning

    sl = SelfLearning(learner, agent_id="ops", framework="langgraph")
    async with sl.turn("the TLS certificate on api.example.com expires tomorrow, rotate it") as t:
        assert "cert-manager" in t.instructions
        t.add_message("assistant", "Rotated.")
        t.outcome = Outcome.success
    sk, _ = await learner.engine.get_skill("acme", "ops", "rotate-tls-cert")
    assert sk.uses == 1 and sk.successes == 1 and t.episode_id


async def test_generic_adapter_records_failure_on_exception(learner):
    from myelinate.adapters.generic import SelfLearning

    sl = SelfLearning(learner, agent_id="ops")
    with pytest.raises(RuntimeError):
        async with sl.turn("rotate the TLS cert"):
            raise RuntimeError("cert-manager not installed")
    sk, _ = await learner.engine.get_skill("acme", "ops", "rotate-tls-cert")
    assert sk.uses == 1 and sk.alpha < sk.beta


async def test_maf_context_provider(learner):
    af = pytest.importorskip("agent_framework")
    from myelinate.adapters.maf import SelfLearningProvider

    seen: dict = {}

    class FakeClient(af.BaseChatClient):
        async def _fake(self, messages, options):
            seen["messages"], seen["options"] = messages, options
            return af.ChatResponse(messages=[af.Message("assistant", ["Certificate rotated."])])

        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            return self._fake(messages, options)

    provider = SelfLearningProvider(learner, agent_id="ops", add_tools=False)
    agent = af.Agent(FakeClient(), instructions="You are an SRE agent.", context_providers=[provider])
    session = agent.create_session()
    session.state["myelinate_user_id"] = "u7"
    result = await agent.run("our TLS certificate is expiring, please rotate it", session=session)
    await provider.flush()

    assert "Certificate rotated." in result.text
    assert "cert-manager" in str(seen["options"]) + str(seen["messages"])
    sk, _ = await learner.engine.get_skill("acme", "ops", "rotate-tls-cert")
    assert sk.uses == 1
