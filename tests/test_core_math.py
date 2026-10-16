from __future__ import annotations

import random

from myelinate.experiments import Arm, Verdict, choose_candidate, decide, lift_interval
from myelinate.retrieval import Candidate, HybridRetriever


def test_decide_promotes_clear_winner_and_rejects_loser():
    inc, cand = Arm(), Arm()
    for i in range(40):
        inc.update(i % 2 == 0)
        cand.update(i % 10 != 0)  # 90% vs 50%
    v, p = decide(inc, cand, min_samples=20, max_samples=300)
    assert v == Verdict.promote and p > 0.95
    v, _ = decide(cand, inc, min_samples=20, max_samples=300)
    assert v == Verdict.reject


def test_decide_waits_for_evidence():
    assert decide(Arm(3, 1), Arm(1, 3), min_samples=20, max_samples=300)[0] == Verdict.continue_


def test_thompson_keeps_exploring_floor():
    rng = random.Random(0)
    strong, weak = Arm(90, 10), Arm(10, 90)
    picks = sum(choose_candidate(strong, weak, rng, floor=0.1) for _ in range(2000))
    assert 150 < picks < 300  # ~10% floor even for a clearly worse candidate


def test_lift_interval_sign():
    mean, lo, hi = lift_interval(Arm(30, 70), Arm(70, 30))
    assert mean > 0.3 and lo > 0


def test_hybrid_mmr_drops_near_duplicates():
    r = HybridRetriever()
    cands = [
        Candidate("a", "deploy service to kubernetes with kubectl rollout"),
        Candidate("b", "deploy service to kubernetes with kubectl rollout status"),
        Candidate("c", "kubernetes namespace pull secret for registry deploy"),
        Candidate("d", "bake a sourdough loaf"),
    ]
    hits = r.search("deploy to kubernetes", cands, None, k=2, min_relevance=0.1)
    keys = [h.key for h in hits]
    assert "d" not in keys and len(keys) == 2 and not {"a", "b"} <= set(keys)


def test_retrieval_handles_paraphrase_abbreviation_and_inflection():
    r = HybridRetriever()
    cands = [Candidate("k8s", "kubernetes prod deploy Use when deploying or updating an application on a production "
                              "Kubernetes cluster"),
             Candidate("pg", "postgres vacuum Reclaim table bloat safely")]
    hits = r.search("roll out payments-api 1.4 to prod k8s", cands, None, k=2, min_relevance=0.12)
    assert [h.key for h in hits] == ["k8s"]
