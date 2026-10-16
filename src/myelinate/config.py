"""Runtime configuration. Every setting can be supplied as an env var prefixed MYELINATE_.

The comment above (or beside) each field is its documentation: scripts/gen_reference.py turns them into
docs/reference/configuration.md, so keep them accurate.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MYELINATE_", env_file=".env", extra="ignore")

    # Deployment mode. Secure by default: outside `dev` the gateway refuses to start without a bootstrap admin
    # key or OIDC. `dev` with no credentials serves everything as tenant "dev" with all scopes (local use only).
    env: Literal["dev", "staging", "prod"] = "prod"
    # SQLAlchemy async URL. SQLite for dev / single node; PostgreSQL (`postgresql+asyncpg://...`) for production.
    database_url: str = "sqlite+aiosqlite:///./myelinate.db"

    # --- LLM used for reflection, curation, judging and evolution ------------
    # `anthropic` uses Claude for learning; `none` disables learning-side LLM calls (recall/record still work).
    llm_provider: Literal["anthropic", "none"] = "anthropic"
    # Anthropic API key. Also read from plain ANTHROPIC_API_KEY (env or .env); unset means the SDK's default lookup.
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("MYELINATE_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"))
    # Model used for learning (not your agent's model).
    llm_model: str = "claude-opus-5-5"
    # Reasoning effort for learning calls. Lower is cheaper; raise for harder domains.
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Output token cap per learning call.
    llm_max_tokens: int = 16000
    # Learning-side tokens per tenant per UTC day; jobs are deferred (not failed) when exceeded. 0 = unlimited.
    daily_token_budget: int = 5_000_000

    # --- Optional embeddings (any /v1/embeddings-compatible endpoint) ---------
    # Embeddings endpoint (Voyage, Azure AI, TEI, vLLM, Ollama, LiteLLM...). Unset = lexical retrieval only.
    embeddings_url: str | None = None
    # Embedding model name sent to the endpoint.
    embeddings_model: str = "voyage-3.5"
    # Bearer token for the embeddings endpoint.
    embeddings_api_key: SecretStr | None = None

    # --- Auth -----------------------------------------------------------------
    # Platform-operator key (send with `X-Tenant-Id`). Use it to mint scoped tenant keys, then vault it.
    bootstrap_admin_key: SecretStr | None = None
    # OIDC issuer URL, checked against the token's `iss`.
    oidc_issuer: str | None = None
    # Expected token audience (`aud`).
    oidc_audience: str | None = None
    # JWKS URL for token signature keys. Setting it enables OIDC authentication.
    oidc_jwks_url: str | None = None
    # Token claim that holds the tenant id (Entra ID: `tid`).
    oidc_tenant_claim: str = "tid"

    # --- Recall ---------------------------------------------------------------
    # Skills injected in full per recall.
    recall_max_skills: int = 3
    # Memories injected per recall.
    recall_max_memories: int = 8
    # Minimum relevance (0-1) for a skill or memory to be recalled.
    recall_min_score: float = 0.12
    # Further matching skills listed by name only (loaded on demand with `skill_view`).
    skill_index_size: int = 10
    # Cap on rules injected per skill (worst-performing rules are dropped first).
    max_bullets_per_skill_injected: int = 40
    # Ask the agent to cite rule ids like [b-1a2b3c4d]: sharper credit assignment, but visible in agent output.
    ask_for_citations: bool = False
    # Share of runs served no learned skills (control group) to measure causal lift. 0 disables.
    holdout_rate: float = 0.05

    # --- Learning loop --------------------------------------------------------
    # Reflect after this many episodes per agent (failures and tool-heavy runs trigger it sooner).
    reflect_every_n_episodes: int = 5
    # Reflect right after a failed episode.
    reflect_on_failure: bool = True
    # Reflect after any episode with at least this many tool calls.
    reflect_min_tool_calls: int = 5
    # Wait for an agent to go quiet before reflecting (debounce), in seconds.
    reflect_debounce_seconds: float = 20.0
    # Use an LLM judge to estimate outcomes when the caller sends none (ReasoningBank-style).
    self_judge_unknown_outcomes: bool = True
    # Below this judge confidence an episode can teach memories but never change skills.
    min_judge_confidence: float = 0.7
    # Maximum edit operations per changeset (bounded "textual learning rate").
    max_ops_per_changeset: int = 6
    # Cosine similarity at which rules are flagged as possible duplicates (when embeddings are configured).
    dedup_similarity: float = 0.85
    # Lexical similarity that flags possible duplicates without embeddings; an LLM decides merge or keep.
    dedup_jaccard: float = 0.3
    # Run library upkeep (dedup, lifecycle, nomination, profiles) every N episodes per agent.
    curate_every_n_episodes: int = 25

    # --- Governance -----------------------------------------------------------
    # Apply new memories without review (still safety-scanned; flagged ones wait for approval).
    auto_approve_memories: bool = True
    # Auto-apply purely additive skill changes (new rules / new skills).
    auto_approve_low_risk: bool = True
    # Auto-apply edits to existing rules and duplicate merges. Keep false for agents that act on production.
    auto_approve_medium_risk: bool = False
    # Changesets with an offline eval score below this are never auto-applied.
    auto_approve_min_eval_score: float = 0.7
    # Maximum characters of new text in one changeset (larger ones are flagged).
    max_skill_chars: int = 16000
    # Rules longer than this many words get a lint warning.
    max_bullet_words: int = 80

    # --- Canary experiments ---------------------------------------------------
    # Serve changes to proven skills beside the current version on live traffic before applying them.
    canary_enabled: bool = True
    # Uses a skill needs before its changes are canaried, and samples per arm before a verdict.
    canary_min_samples: int = 20
    # Samples after which an inconclusive canary keeps the current version.
    canary_max_samples: int = 300
    # Posterior probability needed to promote (or, mirrored, to reject) a candidate.
    canary_threshold: float = 0.95
    # Minimum share of traffic each arm always gets (exploration floor).
    canary_floor: float = 0.1
    # Canaries without a verdict after this many days expire and keep the current version.
    canary_max_days: int = 14

    # --- Evolution (GEPA-style) -----------------------------------------------
    # New uses of a skill required before it can be evolved again.
    evolve_min_uses: int = 10
    # Skills whose posterior success rate is below this are candidates for evolution.
    evolve_max_success_rate: float = 0.7
    # Mutation iterations per evolution run.
    evolve_iterations: int = 4
    # Episodes in each cheap pre-gate minibatch.
    evolve_minibatch: int = 3
    # Held-out episodes used to score candidates.
    evolve_eval_sample: int = 10
    # Held-out score gain a candidate needs over the current skill to be proposed.
    evolve_min_improvement: float = 0.05

    # --- Lifecycle ------------------------------------------------------------
    # Days unused before a skill is marked stale (still recallable, ranked lower).
    stale_after_days: int = 14
    # Days unused before a skill is archived (not recalled; never deleted). Pinned skills are exempt.
    archive_after_days: int = 30
    # Maximum length of a per-user profile card.
    profile_max_chars: int = 1400
    # Share facts learned from one user's runs with all users of the agent (off: they stay user-scoped).
    share_learned_facts: bool = False

    # --- Ops ------------------------------------------------------------------
    # Run the background job worker inside the gateway process. Set false when running `myelinate worker` pods.
    worker_enabled: bool = True
    # Port for Prometheus metrics served by standalone `myelinate worker` processes. 0 disables.
    worker_metrics_port: int = 9464
    # Database connections per process (PostgreSQL), and extra connections allowed under burst.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    # Seconds between job-queue polls when idle.
    worker_poll_seconds: float = 2.0
    # Jobs a worker runs concurrently.
    worker_concurrency: int = 4
    # Attempts before a job is marked failed.
    job_max_attempts: int = 3
    # Requests per minute per authenticated principal (per process).
    rate_limit_per_minute: int = 600
    # Requests per minute per client IP, applied before authentication (per process).
    ip_rate_limit_per_minute: int = 3000
    # Redact emails, phone numbers, card numbers, secrets etc. before anything is stored.
    redact_pii: bool = True
    # Episodes, recall logs and finished jobs older than this are removed by `myelinate purge`.
    episode_retention_days: int = 90
    # Audit-log entries older than this are removed by `myelinate purge`.
    audit_retention_days: int = 365
    # Maximum request body size in bytes (also enforced for chunked uploads).
    max_request_bytes: int = 4_000_000
    # Browser origins allowed by CORS (JSON list). Empty disables CORS.
    cors_origins: list[str] = Field(default_factory=list)
    # Host headers accepted by the /mcp endpoint (DNS-rebinding protection). Add your public hostname.
    mcp_allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost:*", "127.0.0.1:*"])


@lru_cache
def get_settings() -> Settings:
    return Settings()
