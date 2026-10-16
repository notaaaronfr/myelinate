"""Runtime configuration. Every setting can be supplied as an env var prefixed MYELINATE_."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MYELINATE_", env_file=".env", extra="ignore")

    # Secure by default: without credentials the gateway refuses to start unless env=dev is set explicitly.
    env: Literal["dev", "staging", "prod"] = "prod"
    database_url: str = "sqlite+aiosqlite:///./myelinate.db"

    # --- LLM used for reflection, curation, judging and evolution ------------
    llm_provider: Literal["anthropic", "none"] = "anthropic"
    # Read from MYELINATE_ANTHROPIC_API_KEY or plain ANTHROPIC_API_KEY (env or .env). Unset: SDK default lookup.
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("MYELINATE_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"))
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_max_tokens: int = 16000
    daily_token_budget: int = 5_000_000  # per tenant, learning-side only; 0 = unlimited

    # --- Optional embeddings (any /v1/embeddings-compatible endpoint) ---------
    embeddings_url: str | None = None
    embeddings_model: str = "voyage-3.5"
    embeddings_api_key: SecretStr | None = None

    # --- Auth -----------------------------------------------------------------
    bootstrap_admin_key: SecretStr | None = None
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_tenant_claim: str = "tid"

    # --- Recall ---------------------------------------------------------------
    recall_max_skills: int = 3
    recall_max_memories: int = 8
    recall_min_score: float = 0.12
    skill_index_size: int = 10
    max_bullets_per_skill_injected: int = 40
    ask_for_citations: bool = False  # ask the agent to cite [b-xxxx] ids; sharper credit, visible in output
    holdout_rate: float = 0.05  # share of runs served no knowledge, to measure causal lift; 0 disables

    # --- Learning loop --------------------------------------------------------
    reflect_every_n_episodes: int = 5
    reflect_on_failure: bool = True
    reflect_min_tool_calls: int = 5
    reflect_debounce_seconds: float = 20.0  # wait for a session to go quiet before reflecting
    self_judge_unknown_outcomes: bool = True  # ReasoningBank-style LLM judge when no label is given
    min_judge_confidence: float = 0.7  # below this, an episode cannot change skills (memories only)
    max_ops_per_changeset: int = 6  # bounded "textual learning rate" (SkillOpt)
    dedup_similarity: float = 0.85  # cosine (dense) threshold; lexical fallback uses dedup_jaccard
    dedup_jaccard: float = 0.3  # lexical fallback only flags candidates; the LLM decides merge/keep (remembered)
    curate_every_n_episodes: int = 25

    # --- Governance -----------------------------------------------------------
    auto_approve_memories: bool = True
    auto_approve_low_risk: bool = True  # additive edits from verified evidence
    auto_approve_medium_risk: bool = False  # edits/removals of existing rules
    auto_approve_min_eval_score: float = 0.7
    max_skill_chars: int = 16000
    max_bullet_words: int = 80

    # --- Canary experiments ---------------------------------------------------
    canary_enabled: bool = True
    canary_min_samples: int = 20
    canary_max_samples: int = 300
    canary_threshold: float = 0.95
    canary_floor: float = 0.1
    canary_max_days: int = 14  # experiments without a verdict by then are expired (incumbent kept)

    # --- Evolution (GEPA-style) -----------------------------------------------
    evolve_min_uses: int = 10
    evolve_max_success_rate: float = 0.7
    evolve_iterations: int = 4
    evolve_minibatch: int = 3
    evolve_eval_sample: int = 10
    evolve_min_improvement: float = 0.05

    # --- Lifecycle ------------------------------------------------------------
    stale_after_days: int = 14
    archive_after_days: int = 30
    profile_max_chars: int = 1400
    share_learned_facts: bool = False  # facts learned from a user's run stay user-scoped unless enabled

    # --- Ops ------------------------------------------------------------------
    worker_enabled: bool = True
    worker_poll_seconds: float = 2.0
    worker_concurrency: int = 4
    job_max_attempts: int = 3
    rate_limit_per_minute: int = 600
    ip_rate_limit_per_minute: int = 3000  # applied before authentication
    redact_pii: bool = True
    episode_retention_days: int = 90
    max_request_bytes: int = 4_000_000
    cors_origins: list[str] = Field(default_factory=list)
    mcp_allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost:*", "127.0.0.1:*"])


@lru_cache
def get_settings() -> Settings:
    return Settings()
