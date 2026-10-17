"""Signed webhook delivery. Runs as a durable job, so failed deliveries are retried with backoff.

Each request is a JSON POST with:
- `X-Myelinate-Event`: the event name
- `X-Myelinate-Delivery`: a unique id (deduplicate on it; retries reuse it)
- `X-Myelinate-Signature`: `sha256=<hex HMAC of the raw body with MYELINATE_WEBHOOK_SECRET>`
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import httpx


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def deliver(url: str, secret: str | None, event: str, delivery_id: str, data: dict[str, Any],
                  transport: httpx.AsyncBaseTransport | None = None) -> None:
    body = json.dumps({"event": event, "delivery_id": delivery_id, "sent_at": datetime.now(UTC).isoformat(),
                       "data": data}, default=str).encode()
    headers = {"content-type": "application/json", "x-myelinate-event": event, "x-myelinate-delivery": delivery_id,
               "user-agent": "myelinate-webhooks"}
    if secret:
        headers["x-myelinate-signature"] = sign(secret, body)
    async with httpx.AsyncClient(timeout=15.0, transport=transport) as http:
        r = await http.post(url, content=body, headers=headers)
    if r.status_code >= 300:
        raise RuntimeError(f"webhook {event} to {url} returned {r.status_code}")
