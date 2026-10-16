# Security policy

Myelinate stores agent transcripts, learned skills and user memories, and learned skills are injected into future
prompts, so a poisoned skill is a persistent prompt injection. Security reports are taken seriously.

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub security advisories](https://github.com/notaaaronfr/myelinate/security/advisories/new). Don't open a public
issue. You'll get an acknowledgement within a few days.

## Hardening checklist for deployments

- Run with `MYELINATE_ENV=prod` (the default). The gateway refuses to start without credentials.
- Use OIDC or per-tenant API keys with the minimum scopes (`learn` for agents, `review` for humans, `admin` rarely).
- Keep `MYELINATE_AUTO_APPROVE_MEDIUM_RISK=false` for agents that act on production systems, and review changesets.
- Use PostgreSQL with TLS and encryption at rest. Set `MYELINATE_MCP_ALLOWED_HOSTS` to your public hostname.
- Put a shared rate limiter or API gateway in front of multiple replicas.
