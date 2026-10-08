"""Redis fast path for idempotent replays (BUILDBOOK 7.5).

Redis is only a cache: the UNIQUE idempotency_key in Postgres is the real guarantee.
Every Redis call is wrapped so that a Redis outage degrades to the DB path, never an error.
"""

import json
import logging

from fastapi import Request
from redis.asyncio import Redis

from app.config import settings

log = logging.getLogger(__name__)


def get_redis(request: Request) -> Redis:
    """FastAPI dependency: the process-wide Redis client created in the lifespan."""
    return request.app.state.redis


def _redis_key(idem_key: str) -> str:
    return f"idem:{idem_key}"


async def get_cached(redis: Redis, idem_key: str) -> dict | None:
    """Return {request_hash, status_code, body} for this key, or None (miss or Redis down)."""
    try:
        raw = await redis.get(_redis_key(idem_key))
    except Exception as exc:
        log.warning("idempotency: redis GET failed, falling back to DB: %r", exc)
        return None
    if raw is None:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        entry = None
    if not isinstance(entry, dict) or not {"request_hash", "body"} <= entry.keys():
        log.warning("idempotency: corrupt cache entry for key %r, ignoring", idem_key)
        return None
    return entry


async def cache_response(redis: Redis, idem_key: str, request_hash: str, status_code: int, body: dict) -> None:
    """Store a successful reserve response. Failures are logged and ignored."""
    value = json.dumps({"request_hash": request_hash, "status_code": status_code, "body": body})
    try:
        await redis.set(_redis_key(idem_key), value, ex=settings.IDEMPOTENCY_TTL_SECONDS)
    except Exception as exc:
        log.warning("idempotency: redis SET failed, continuing without cache: %r", exc)
