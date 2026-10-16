"""Online validation of learned knowledge.

Offline LLM-judge scores are a proxy. What matters is whether real runs succeed more often, so:
- Canary: a changed skill is served beside the incumbent version, chosen by Thompson sampling with a
  guaranteed exploration floor. It is promoted or rejected automatically once the posterior is decisive.
- Holdout: a small share of runs gets no learned knowledge. Comparing them with treated runs gives the
  causal "learning lift", the number that shows whether the system is paying for itself.

Rewards are in [0, 1] (success=1, failure=0, partial=0.5, explicit feedback = its score) and update a
Beta(alpha, beta) posterior with fractional pseudo-counts.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum


@dataclass
class Arm:
    alpha: float = 1.0
    beta: float = 1.0

    @property
    def n(self) -> float:
        return self.alpha + self.beta - 2.0

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def update(self, reward: float) -> None:
        r = min(1.0, max(0.0, reward))
        self.alpha += r
        self.beta += 1.0 - r

    def sample(self, rng: random.Random) -> float:
        return rng.betavariate(self.alpha, self.beta)


def prob_better(a: Arm, b: Arm, draws: int = 4000, seed: int = 7) -> float:
    """P(b > a) by Monte Carlo over the two Beta posteriors."""
    rng = random.Random(seed)
    return sum(b.sample(rng) > a.sample(rng) for _ in range(draws)) / draws


def credible_interval(arm: Arm, level: float = 0.9, draws: int = 4000, seed: int = 11) -> tuple[float, float]:
    rng = random.Random(seed)
    xs = sorted(arm.sample(rng) for _ in range(draws))
    lo = xs[int((1 - level) / 2 * draws)]
    hi = xs[int((1 + level) / 2 * draws) - 1]
    return lo, hi


def lift_interval(control: Arm, treated: Arm, level: float = 0.9, draws: int = 4000,
                  seed: int = 13) -> tuple[float, float, float]:
    """Posterior (mean, lo, hi) of treated - control success rate."""
    rng = random.Random(seed)
    d = sorted(treated.sample(rng) - control.sample(rng) for _ in range(draws))
    return sum(d) / draws, d[int((1 - level) / 2 * draws)], d[int((1 + level) / 2 * draws) - 1]


def choose_candidate(incumbent: Arm, candidate: Arm, rng: random.Random, floor: float = 0.1) -> bool:
    """Thompson sampling with an exploration floor in both directions. True means serve the candidate."""
    u = rng.random()
    if u < floor:
        return True
    if u < 2 * floor:
        return False
    return candidate.sample(rng) > incumbent.sample(rng)


class Verdict(StrEnum):
    continue_ = "continue"
    promote = "promote"
    reject = "reject"


def decide(incumbent: Arm, candidate: Arm, min_samples: float, max_samples: float,
           threshold: float = 0.95) -> tuple[Verdict, float]:
    if candidate.n < min_samples or incumbent.n < min_samples:
        return Verdict.continue_, 0.5
    p = prob_better(incumbent, candidate)
    if p >= threshold:
        return Verdict.promote, p
    if p <= 1 - threshold:
        return Verdict.reject, p
    if candidate.n >= max_samples:
        # Inconclusive after the budget: keep the proven version (conservative default).
        return Verdict.reject, p
    return Verdict.continue_, p
