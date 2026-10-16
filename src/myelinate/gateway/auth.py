"""Authentication and authorisation.

Three credential types, all sent as `Authorization: Bearer <token>`:
- Tenant API keys (`myl_...`): stored as SHA-256 hashes, scoped to one tenant.
- OIDC JWTs (Entra ID, Okta, Auth0, Keycloak...): tenant from a claim, scopes from `scp`/`scope`/`roles`.
- The bootstrap admin key: platform operator only; must name the tenant via `X-Tenant-Id`.

Scopes: `learn` (recall, record, feedback, read knowledge), `review` (approve proposals, author and
roll back skills), `admin` (API keys, audit log, data erasure).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

import jwt
import structlog
from sqlalchemy import select

from ..config import Settings
from ..db import ApiKey, Database

log = structlog.get_logger()

ALL_SCOPES = frozenset({"learn", "review", "admin"})
KEY_PREFIX = "myl_"


class AuthError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject: str
    scopes: frozenset[str] = field(default_factory=frozenset)

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise AuthError(403, f"missing scope '{scope}'")


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


class Authenticator:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.s, self.db = settings, db
        self._jwks = jwt.PyJWKClient(settings.oidc_jwks_url, cache_keys=True) if settings.oidc_jwks_url else None
        self._cache: dict[str, tuple[float, Principal | None]] = {}
        self.anonymous_dev = (settings.env == "dev" and settings.bootstrap_admin_key is None and self._jwks is None)
        if settings.env == "prod" and settings.bootstrap_admin_key is None and self._jwks is None:
            raise RuntimeError("prod requires MYELINATE_BOOTSTRAP_ADMIN_KEY or OIDC configuration")
        if self.anonymous_dev:
            log.warning("auth.disabled", reason="dev mode with no credentials configured; tenant='dev'")

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token:
            if self.anonymous_dev:
                return Principal("dev", "anonymous", ALL_SCOPES)
            raise AuthError(401, "missing bearer token")

        boot = self.s.bootstrap_admin_key
        if boot is not None and hmac.compare_digest(token, boot.get_secret_value()):
            tenant = headers.get("x-tenant-id") or headers.get("X-Tenant-Id")
            if not tenant:
                raise AuthError(400, "bootstrap admin key requires X-Tenant-Id")
            return Principal(tenant, "bootstrap-admin", ALL_SCOPES)

        if token.startswith(KEY_PREFIX):
            return await self._api_key(token)
        if self._jwks is not None:
            return await asyncio.to_thread(self._jwt, token)
        raise AuthError(401, "invalid credentials")

    async def _api_key(self, token: str) -> Principal:
        h = hash_key(token)
        hit = self._cache.get(h)
        if hit and hit[0] > time.monotonic():
            if hit[1] is None:
                raise AuthError(401, "invalid api key")
            return hit[1]
        async with self.db.session() as ses:
            row = await ses.scalar(select(ApiKey).where(ApiKey.key_hash == h))
        p = None if row is None or row.revoked else Principal(row.tenant_id, f"key:{row.id}",
                                                             frozenset(row.scopes) & ALL_SCOPES)
        if len(self._cache) > 10_000:  # bounded: random invalid tokens can't grow memory
            self._cache.clear()
        self._cache[h] = (time.monotonic() + 30, p)  # short TTL bounds revocation delay
        if p is None:
            raise AuthError(401, "invalid api key")
        return p

    def _jwt(self, token: str) -> Principal:
        assert self._jwks is not None
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256", "ES256", "PS256"], audience=self.s.oidc_audience,
                                issuer=self.s.oidc_issuer, options={"require": ["exp", "iss", "sub"]})
        except jwt.PyJWTError as e:
            raise AuthError(401, f"invalid token: {e}") from e
        tenant = claims.get(self.s.oidc_tenant_claim)
        if not tenant:
            raise AuthError(403, f"token has no '{self.s.oidc_tenant_claim}' claim")
        raw = claims.get("scp") or claims.get("scope") or ""
        scopes = set(raw.split() if isinstance(raw, str) else raw) | set(claims.get("roles", []))
        scopes = {s.removeprefix("myelinate.") for s in scopes}
        return Principal(str(tenant), f"oidc:{claims['sub']}", frozenset(scopes) & ALL_SCOPES)


class RateLimiter:
    """Per-principal token bucket. For many replicas put a shared limiter (API gateway / Redis) in front."""

    def __init__(self, per_minute: int) -> None:
        self.rate = per_minute / 60.0
        self.cap = float(per_minute)
        self._b: dict[str, tuple[float, float]] = {}

    def check(self, key: str) -> None:
        if self.rate <= 0:
            return
        now = time.monotonic()
        tokens, last = self._b.get(key, (self.cap, now))
        tokens = min(self.cap, tokens + (now - last) * self.rate)
        if tokens < 1:
            raise AuthError(429, "rate limit exceeded")
        if len(self._b) > 100_000:
            self._b.clear()
        self._b[key] = (tokens - 1, now)
