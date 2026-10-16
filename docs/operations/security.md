# Security

This page is for platform, security and SRE teams running Myelinate. It covers the threat model, authentication,
authorisation, tenant isolation, request limits and how to place the gateway on a network. To report a vulnerability,
see [SECURITY.md](https://github.com/notaaaronfr/myelinate/blob/main/SECURITY.md).

## Threat model

Myelinate stores agent transcripts, learned skills and user memories, and it **injects learned skills and memories
into future prompts**. That makes it a persistence layer for prompt content: a poisoned skill or memory is a
*persistent prompt injection* that reaches every later run of that agent, and every agent in the tenant if it lands
in the shared `agent_id="*"` namespace (which only `review`-scoped skill authoring can write).

| Asset | Threat | Main controls |
|---|---|---|
| Learned skills (rules injected into prompts) | Poisoning through crafted episodes, feedback or OTLP spans; malicious authored or imported SKILL.md | Evidence gating, safety scan, risk tiers, human review, canary, versions and rollback |
| Memories and profile cards | Poisoned facts; cross-user leakage | Safety scan (flagged memories go to `pending`), user scoping, `share_learned_facts=false` |
| Transcripts (episodes) | PII and secrets at rest; exfiltration | Regex redaction before storage, retention purge, erasure, tenant scoping |
| Credentials | Key theft, replay | Hashed API keys, revocation, short-lived OIDC tokens, TLS |
| LLM spend | Cost exhaustion by flooding episodes | Per-tenant daily token budget, rate limits |

### How a poisoned change is caught

Every change to a skill is a **changeset**: a bounded list of typed operations. It passes through these layers in
order (details in [Governance](governance.md)):

1. **Evidence gating.** Reflection only proposes skill changes from episodes with a reliable outcome signal
   (verification, feedback, a reported outcome, or a judge verdict above `min_judge_confidence`). Episodes without one
   can add memories but never change skills.
2. **Safety scan** (`safety.scan`). It looks for secrets (AWS keys, private keys, `sk-`/`pk-`/`rk-` style keys, GitHub
   and Slack tokens, JWTs, `password=` style assignments), prompt-injection phrases ("ignore previous instructions",
   "reveal the system prompt", `curl … | sh`, `rm -rf /`, "disable the guardrails", "do not tell the user", and
   similar), and oversize content (`max_skill_chars`). **Any finding blocks auto-approval.** Findings are stored on the
   changeset and shown to reviewers.
3. **Risk tier.** Purely additive changes are `low`, edits are `medium`, and removals or description (routing)
   changes are `high`. High risk always needs a human.
4. **Human review** for anything policy doesn't auto-approve.
5. **Canary.** A medium- or high-risk change to a proven skill is served beside the incumbent on live traffic and is
   promoted only if it performs better.
6. **Versions and rollback.** Every applied change creates an immutable version, and `POST /v1/skills/{id}/rollback`
   restores any earlier one.

!!! warning "Know the limits of these controls"
    - The injection scan is a short list of regular expressions. It catches careless or obvious attacks, not a
      determined adversary. Treat human review as the real control for anything that matters.
    - With the default policy (`auto_approve_low_risk=true`), **purely additive changes, including brand-new skills,
      apply without human review** if the scan finds nothing. Reflection counts a *reported* outcome label as strong
      evidence, so any caller with the `learn` scope can steer what gets learned. In regulated environments set
      `MYELINATE_AUTO_APPROVE_LOW_RISK=false`.
    - The tenant-wide shared namespace `agent_id="*"` is rejected (`422`) in every `learn`-scoped request body
      (recall, episodes, memories, feedback, ask). Only `POST /v1/skills` and `POST /v1/skills/import`, which need
      `review`, accept it, so shared knowledge can only be published by reviewers. Agents still *read* shared skills
      and memories.
    - `user_id` is supplied by the caller and is **not** an authentication boundary. Any credential in a tenant can
      read and write any user's memories in that tenant. The tenant is the security boundary.

## Authentication

Every credential is sent as `Authorization: Bearer <token>`. Three kinds are accepted.

### Bootstrap admin key

`MYELINATE_BOOTSTRAP_ADMIN_KEY` is a platform-operator credential with all scopes in **any** tenant. Because it isn't
bound to a tenant, every request must name one with `X-Tenant-Id`. Without it the gateway returns
`400 bootstrap admin key requires X-Tenant-Id`.

```bash
curl -s https://learner.internal/v1/admin/api-keys \
  -H "Authorization: Bearer $MYELINATE_BOOTSTRAP_ADMIN_KEY" \
  -H "X-Tenant-Id: acme" \
  -H "Content-Type: application/json" \
  -d '{"name": "support-bot-prod", "scopes": ["learn"]}'
```

The comparison is constant-time. All bootstrap requests share one rate-limit bucket (`bootstrap-admin`). Use the key to
onboard tenants (mint their first keys), then keep it in a vault. If you use OIDC, you don't have to set it at all.

### Tenant API keys

- Format: `myl_` followed by 43 URL-safe random characters (32 bytes of entropy).
- Minted with `POST /v1/admin/api-keys` (needs the `admin` scope). The key belongs to the caller's tenant. The raw key is
  returned **once**, and only its SHA-256 hash is stored.
- Scopes default to `["learn"]`. An unknown scope returns `400`.
- Revoke with `DELETE /v1/admin/api-keys/{key_id}` (`admin`). The `id` comes from the create response and from the
  `apikey.created` audit entry.
- **Revocation delay:** each gateway process caches key lookups, valid and invalid alike, for **30 seconds**. A revoked
  key keeps working for up to 30 s on processes that had it cached.
- There is no endpoint that lists keys. Keep an inventory from the create responses or the audit log.

Rotation: mint a new key, deploy it to the client, then revoke the old one.

### OIDC / JWT

Set `MYELINATE_OIDC_JWKS_URL` to enable JWT validation. Outside `MYELINATE_ENV=dev`, it also **requires**
`MYELINATE_OIDC_ISSUER` and `MYELINATE_OIDC_AUDIENCE`. Without both, the gateway refuses to start
(`RuntimeError: OIDC requires MYELINATE_OIDC_ISSUER and MYELINATE_OIDC_AUDIENCE outside dev`), because otherwise any
token signed by the same keys, for example one issued for another application, would be accepted.

Tokens are validated as follows (`gateway/auth.py`):

| Check | Behaviour |
|---|---|
| Signature | Key from the JWKS (cached). Algorithms `RS256`, `ES256` and `PS256` only; HMAC tokens are rejected. |
| Required claims | `exp`, `iss`, `sub` |
| Issuer | Must equal `MYELINATE_OIDC_ISSUER` exactly |
| Audience | `aud` must contain `MYELINATE_OIDC_AUDIENCE` |
| Tenant | Read from the claim named by `MYELINATE_OIDC_TENANT_CLAIM` (default `tid`). A missing claim returns `403`. |
| Scopes | The union of `scp` or `scope`, `roles` (Entra ID app roles), `permissions` (Auth0 RBAC) and `realm_access.roles` (Keycloak). Each may be a space-separated string or a list. A `myelinate.` prefix is stripped, so `myelinate.learn` and `learn` are equivalent. Anything other than `learn`, `review` or `admin` is ignored. |

Only one issuer is supported. Multi-tenant "common" endpoints whose issuer varies per directory are not supported.

#### Microsoft Entra ID

Register an API app, set `"requestedAccessTokenVersion": 2` in its manifest, and expose either delegated scopes
(`learn`, `review`, `admin`) or app roles with the same values (app roles appear in `roles` for client-credential
flows). `tid` (the Entra directory ID) becomes the Myelinate tenant.

```bash
MYELINATE_OIDC_ISSUER=https://login.microsoftonline.com/<directory-id>/v2.0
MYELINATE_OIDC_JWKS_URL=https://login.microsoftonline.com/<directory-id>/discovery/v2.0/keys
MYELINATE_OIDC_AUDIENCE=<application-client-id>      # v2 tokens carry the client ID GUID as aud
MYELINATE_OIDC_TENANT_CLAIM=tid
```

With v1 tokens the issuer is `https://sts.windows.net/<directory-id>/` and `aud` is the App ID URI
(`api://myelinate`). Pick one token version and configure to match. Because `tid` is the same for every user in a
directory, one Entra directory maps to one Myelinate tenant. To split teams into separate tenants, emit a custom claim
(for example `myelinate_tenant`) and point `MYELINATE_OIDC_TENANT_CLAIM` at it.

#### Okta

Use a custom authorization server and define scopes `myelinate.learn`, `myelinate.review` and `myelinate.admin`. Okta
puts them in `scp` as a list, which is supported. Add a custom access-token claim for the tenant.

```bash
MYELINATE_OIDC_ISSUER=https://<your-org>.okta.com/oauth2/<auth-server-id>    # e.g. .../oauth2/default
MYELINATE_OIDC_JWKS_URL=https://<your-org>.okta.com/oauth2/<auth-server-id>/v1/keys
MYELINATE_OIDC_AUDIENCE=api://myelinate
MYELINATE_OIDC_TENANT_CLAIM=tenant
```

#### Auth0

Create an API whose identifier is the audience, and define permissions/scopes `learn`, `review`, `admin`. Auth0 puts
granted scopes in `scope`. If you enable RBAC with "Add Permissions in the Access Token", the `permissions` claim is
read too. Add the tenant with an Action, using a namespaced claim. Note the trailing slash on the issuer.

```bash
MYELINATE_OIDC_ISSUER=https://<your-tenant>.auth0.com/
MYELINATE_OIDC_JWKS_URL=https://<your-tenant>.auth0.com/.well-known/jwks.json
MYELINATE_OIDC_AUDIENCE=https://learner.example.com
MYELINATE_OIDC_TENANT_CLAIM=https://learner.example.com/tenant
```

#### Keycloak

Create client scopes named `learn`, `review`, `admin` (or with the `myelinate.` prefix). Add an **Audience** mapper so
`aud` contains your API client. Keycloak's default `aud` is `account`. Realm roles in `realm_access.roles` are read
too, so assigning realm roles `learn`, `review` or `admin` (or the `myelinate.`-prefixed forms) also works. Client
roles under `resource_access` are not read. Add a hardcoded-claim or user-attribute mapper for the tenant.

```bash
MYELINATE_OIDC_ISSUER=https://keycloak.example.com/realms/<realm>
MYELINATE_OIDC_JWKS_URL=https://keycloak.example.com/realms/<realm>/protocol/openid-connect/certs
MYELINATE_OIDC_AUDIENCE=myelinate
MYELINATE_OIDC_TENANT_CLAIM=tenant
```

JWT validation is implemented and unit-tested, but it has **not** been tested against a live Entra ID or Okta
tenant. Validate your configuration in staging with a real token before rollout.

## Authorisation: scopes

There are three scopes, and they are **not hierarchical**: `admin` doesn't imply `review`, and `review` doesn't imply
`learn`. Grant every scope a principal needs, for example `["learn", "review"]` for a reviewer who also reads skills.

| Scope | Intended for | Endpoints |
|---|---|---|
| `learn` | Agents and adapters | `POST /v1/recall`, `POST /v1/episodes`, `POST /v1/episodes:batch`, `GET /v1/episodes/latest`, `POST /v1/feedback`, `POST /v1/traces`, `POST /otlp/v1/traces`, `POST /v1/ask`, `GET /v1/skills`, `GET /v1/skills/{ref}`, `GET /v1/skills/{ref}/SKILL.md`, `GET /v1/skills/{ref}/versions`, `GET /v1/memories`, `POST /v1/memories`, `GET /v1/profiles/{agent_id}/{user_id}`, `GET /v1/agents/{agent_id}/insights`, and every MCP tool |
| `review` | Humans who govern knowledge | `GET /v1/changesets`, `POST /v1/changesets/{id}/review`, `POST /v1/skills`, `POST /v1/skills/import`, `POST /v1/skills/{id}/rollback`, `POST /v1/skills/{id}/pin`, `POST /v1/skills/{id}/evolve`, `POST /v1/memories/{id}/approve`, `DELETE /v1/memories/{id}`, `POST /v1/agents/{id}/reflect`, `POST /v1/agents/{id}/curate`, `POST /v1/agents/{id}/replay`, `GET /v1/agents/{id}/export` |
| `admin` | Tenant administrators, rarely | `POST /v1/admin/api-keys`, `DELETE /v1/admin/api-keys/{id}`, `GET /v1/admin/audit`, `GET /v1/admin/usage`, `DELETE /v1/admin/users/{user_id}` |
| none | Infrastructure | `/healthz`, `/readyz`, `/metrics`, `/docs`, `/openapi.json`. The `/mcp` transport itself is open, but each tool call authenticates. |

!!! warning
    `/metrics`, `/docs` and `/openapi.json` are unauthenticated. Don't expose them publicly; restrict them at the
    ingress (see [Network placement](#network-placement)).

Note that `GET /v1/agents/{id}/export` (`review`) returns full, redacted transcripts. Treat `review` as a privileged
scope.

## Multi-tenancy and isolation

- The tenant always comes from the credential: the API key's tenant, the JWT tenant claim, or `X-Tenant-Id` for the
  bootstrap key. It is never read from request bodies.
- Every table has a `tenant_id` column, and every query filters by it. Resources fetched by ID (changesets, skills,
  memories, episodes, keys) are checked against the caller's tenant and return `404` otherwise.
- Within a tenant, knowledge is scoped per `agent_id`. Skills and memories under `agent_id="*"` are shared by all
  agents in the tenant. Only `review`-scoped skill authoring and import can write there. When names clash, an agent's own skill wins over a shared one.
- Tenants aren't registered anywhere. A tenant exists as soon as a credential names it.
- All tenants share one database, one worker pool and one LLM account. Isolation is logical (row-level filtering in
  application code), not physical. If you need physical isolation, run separate deployments with separate databases.

## Rate limits

There are two token-bucket limiters, both in process memory:

| Limiter | Setting | Default | Key | Applied |
|---|---|---|---|---|
| Per-IP | `MYELINATE_IP_RATE_LIMIT_PER_MINUTE` | 3000 | client IP | Before authentication, on every route including `/healthz`, `/metrics` and `/mcp` |
| Per-principal | `MYELINATE_RATE_LIMIT_PER_MINUTE` | 600 | API key ID, `oidc:<sub>`, or `bootstrap-admin` | After authentication, REST and MCP |

Exceeding either returns `429 {"detail": "rate limit exceeded"}` with `Retry-After: 60`. A value of `0`
disables a limiter.

- **Limits are per process.** With N replicas × M uvicorn workers, the effective limit is up to N × M times the
  setting. Put a shared limiter (API gateway, ingress or Redis-backed) in front of multi-replica deployments.
- **Client IP behind a proxy.** Uvicorn trusts `X-Forwarded-For` only from addresses in the `FORWARDED_ALLOW_IPS`
  environment variable (default `127.0.0.1,::1`). Behind a load balancer on another address, every request appears to
  come from the load balancer, so all clients share one IP bucket. Set `FORWARDED_ALLOW_IPS` to your proxy's address
  or CIDR.

## Request size and input limits

- `MYELINATE_MAX_REQUEST_BYTES` (default 4,000,000) caps request bodies, including chunked bodies without
  `Content-Length`. Oversized requests get `413`.
- `POST /v1/episodes:batch` accepts at most 1000 episodes (`413`).
- Field limits return `422`: recall `query` ≤ 100,000 chars, memory `content` ≤ 4000, SKILL.md import ≤ 200,000,
  skill `description` ≤ 1024, bullet `content` 3–2000. IDs follow fixed patterns (`agent_id`:
  `^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$`, with `*` additionally allowed only in skill authoring and import; skill
  names: `^[a-z0-9][a-z0-9-]{1,63}$`).

## MCP DNS-rebinding protection

The MCP endpoint (`/mcp`) validates the `Host` header against `MYELINATE_MCP_ALLOWED_HOSTS` and the `Origin` header
(when present) against `MYELINATE_CORS_ORIGINS`. The default allows only `localhost:*` and `127.0.0.1:*`, so **every
real deployment must set it**. Otherwise MCP clients get `421 Invalid Host header`, and browsers sending a foreign
`Origin` get `403 Invalid Origin header`.

```bash
# JSON list. Entries match the Host header exactly; "host:*" matches any port.
MYELINATE_MCP_ALLOWED_HOSTS='["learner.example.com","myelinate.ml-platform.svc.cluster.local:*"]'
```

List every name clients use to reach the gateway, including in-cluster service names.

## Secure by default

- `MYELINATE_ENV` defaults to `prod`. In `prod` the gateway **refuses to start** without
  `MYELINATE_BOOTSTRAP_ADMIN_KEY` or `MYELINATE_OIDC_JWKS_URL`
  (`RuntimeError: prod requires MYELINATE_BOOTSTRAP_ADMIN_KEY or OIDC configuration`).
- Only `MYELINATE_ENV=dev` with **no** credentials configured disables auth. All requests then run as tenant `dev`
  with all scopes, and a `auth.disabled` warning is logged. `staging` starts without credentials, but then rejects
  every request with `401`.
- Logs are JSON in `staging` and `prod`, and human-readable console output in `dev`.
- PII and secret redaction (`MYELINATE_REDACT_PII`) is on by default. Learned facts are user-scoped
  (`MYELINATE_SHARE_LEARNED_FACTS=false`). Medium- and high-risk skill changes need a human by default.

## Secrets handling

| Secret | Setting | Notes |
|---|---|---|
| Bootstrap admin key | `MYELINATE_BOOTSTRAP_ADMIN_KEY` | Long random string (for example `openssl rand -base64 48`). Rotate by changing it and restarting. |
| Anthropic API key | `MYELINATE_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY` | Either name works |
| Embeddings key | `MYELINATE_EMBEDDINGS_API_KEY` | Optional |
| Database password | inside `MYELINATE_DATABASE_URL` | |

- Settings are read from environment variables and from a `.env` file in the working directory. In containers, inject
  them from a secret manager (Kubernetes Secrets, Vault, AWS/Azure/GCP secret stores) rather than baking a `.env` into
  images. The shipped `Dockerfile` doesn't copy `.env`.
- Secret settings are `SecretStr` and aren't printed in logs or reprs.
- Tenant API keys are stored only as SHA-256 hashes. The database never holds a usable key.

## Network placement

- **TLS.** The gateway serves plain HTTP. Terminate TLS at an ingress, load balancer or service mesh. `myelinate serve`
  enables proxy headers, so set `FORWARDED_ALLOW_IPS` (see above).
- **Private networking.** Run the gateway on a private network reachable by your agents and reviewers. Expose `/v1/*`
  and `/mcp` through the ingress, and keep `/metrics` reachable only from your Prometheus.
- **Egress.** Workers and gateways need outbound HTTPS to the Anthropic API (or to the endpoint in
  `ANTHROPIC_BASE_URL`, which the Anthropic SDK honours) and to `MYELINATE_EMBEDDINGS_URL` if set. Nothing else.
- **Database.** Use PostgreSQL with TLS (`postgresql+asyncpg://…?ssl=require`) and encryption at rest. Restrict
  network access to gateway and worker pods.
- **CORS** is off unless `MYELINATE_CORS_ORIGINS` is set. Most deployments don't need it.

## Hardening checklist

- [ ] `MYELINATE_ENV=prod` (the default); OIDC or per-tenant keys; bootstrap key vaulted or unset.
- [ ] Least-privilege scopes: `learn` for agents, `learn`+`review` for reviewers, `admin` for few people.
- [ ] `MYELINATE_AUTO_APPROVE_MEDIUM_RISK=false`. Also set `MYELINATE_AUTO_APPROVE_LOW_RISK=false` for agents that act
      on production systems.
- [ ] `MYELINATE_MCP_ALLOWED_HOSTS` set to your hostnames.
- [ ] PostgreSQL with TLS and encryption at rest; backups ([Backup and upgrades](backup-and-upgrades.md)).
- [ ] Shared rate limiter in front of multiple replicas; `FORWARDED_ALLOW_IPS` set.
- [ ] `/metrics` and `/docs` not publicly reachable.
- [ ] `myelinate purge` scheduled ([Data and privacy](data-and-privacy.md)).

See also: [Governance](governance.md), [Data and privacy](data-and-privacy.md),
[Configuration reference](../reference/configuration.md), [API reference](../reference/api.md).
