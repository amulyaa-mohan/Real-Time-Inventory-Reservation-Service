"""Reserve logic (BUILDBOOK 7.1): optimistic locking with bounded, jittered retries.

Redis fast path (step 1) is added in Phase 3; confirm/release in Phase 4.
"""

import asyncio
import hashlib
import logging
import random
import uuid
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.errors import AppError, not_found

log = logging.getLogger(__name__)

RESERVATION_COLUMNS = "id, product_id, qty, status, expires_at, created_at"


class _VersionConflict(Exception):
    """Another request changed the product row between our SELECT and UPDATE."""


def request_hash(product_id: int, qty: int) -> str:
    return hashlib.sha256(f"{product_id}:{qty}".encode()).hexdigest()


async def _find_by_key(engine: AsyncEngine, idem_key: str) -> dict | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(f"""
                    SELECT {RESERVATION_COLUMNS}, request_hash
                      FROM reservations
                     WHERE idempotency_key = :key
                """),
                {"key": idem_key},
            )
        ).mappings().first()
    return dict(row) if row else None


def _replay(existing: dict, req_hash: str) -> dict:
    """Return a stored reservation for a repeated key, if the body matches."""
    if existing["request_hash"] != req_hash:
        raise AppError(
            422,
            "IDEMPOTENCY_KEY_REUSED",
            "this Idempotency-Key was already used with a different request body",
        )
    return {k: v for k, v in existing.items() if k != "request_hash"}


async def _try_reserve(engine: AsyncEngine, product_id: int, qty: int, idem_key: str, req_hash: str) -> dict:
    """One attempt. Decrement + INSERT share one transaction: both commit or neither does."""
    async with engine.begin() as conn:
        product = (
            await conn.execute(
                text("SELECT available, version FROM products WHERE id = :pid"),
                {"pid": product_id},
            )
        ).mappings().first()
        if product is None:
            raise not_found("product")
        if product["available"] < qty:
            raise AppError(409, "OUT_OF_STOCK", f"only {product['available']} available")

        # Optimistic lock: succeeds only if nobody changed the row since our SELECT.
        # `available >= :qty` is a second guard so stock can never go negative here,
        # and CHECK (available >= 0) in schema.sql is the third.
        updated = await conn.execute(
            text("""
                UPDATE products
                   SET available = available - :qty,
                       version = version + 1,
                       updated_at = now()
                 WHERE id = :pid AND version = :v AND available >= :qty
            """),
            {"pid": product_id, "qty": qty, "v": product["version"]},
        )
        if updated.rowcount == 0:
            raise _VersionConflict  # leaving the block rolls back

        # A concurrent request with the same Idempotency-Key makes this INSERT fail
        # with IntegrityError (UNIQUE idempotency_key), which also rolls back the decrement.
        row = (
            await conn.execute(
                text(f"""
                    INSERT INTO reservations
                        (id, product_id, qty, status, idempotency_key, request_hash, expires_at)
                    VALUES
                        (:id, :pid, :qty, 'HELD', :key, :hash, now() + CAST(:ttl AS interval))
                    RETURNING {RESERVATION_COLUMNS}
                """),
                {
                    "id": uuid.uuid4(),
                    "pid": product_id,
                    "qty": qty,
                    "key": idem_key,
                    "hash": req_hash,
                    "ttl": timedelta(seconds=settings.HOLD_TTL_SECONDS),
                },
            )
        ).mappings().one()
        return dict(row)


async def reserve(engine: AsyncEngine, product_id: int, qty: int, idem_key: str) -> tuple[dict, bool]:
    """Returns (reservation, replayed). replayed=True means an earlier result was returned."""
    req_hash = request_hash(product_id, qty)

    # Step 2: this key was already used -> return the original result (or 422).
    existing = await _find_by_key(engine, idem_key)
    if existing:
        return _replay(existing, req_hash), True

    # Step 3: optimistic-lock attempts with jittered exponential backoff.
    for attempt in range(1, settings.MAX_RESERVE_RETRIES + 1):
        try:
            return await _try_reserve(engine, product_id, qty, idem_key, req_hash), False
        except _VersionConflict:
            if attempt < settings.MAX_RESERVE_RETRIES:
                # Random sleep in [0, 5ms * 2^attempt) so colliding requests spread out
                # instead of retrying in lockstep and colliding again.
                await asyncio.sleep(random.uniform(0, 0.005 * 2**attempt))
        except IntegrityError:
            # Lost a race with a concurrent request using the same key. Postgres made
            # our INSERT wait until the winner committed, so the winner's row is visible now.
            existing = await _find_by_key(engine, idem_key)
            if existing is None:
                raise  # some other integrity failure; surface it as a 500
            return _replay(existing, req_hash), True

    # Step 4: too much contention on this product.
    log.warning("reserve: retries exhausted for product %s", product_id)
    raise AppError(503, "BUSY", "too much contention, retry shortly", headers={"Retry-After": "1"})


async def get_reservation(engine: AsyncEngine, reservation_id: uuid.UUID) -> dict:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(f"SELECT {RESERVATION_COLUMNS} FROM reservations WHERE id = :id"),
                {"id": reservation_id},
            )
        ).mappings().first()
    if row is None:
        raise not_found("reservation")
    return dict(row)
