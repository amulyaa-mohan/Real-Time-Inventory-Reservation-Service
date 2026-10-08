"""Background expiry of abandoned holds (BUILDBOOK 7.4)."""

import asyncio
import logging
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)


async def _reap_batch(engine: AsyncEngine, batch_size: int) -> int:
    """Expire up to batch_size overdue holds and return their stock, in one transaction."""
    async with engine.begin() as conn:
        # FOR UPDATE SKIP LOCKED: rows another reaper (or a confirm/release) has locked
        # are skipped instead of waited on, so replicas split the work, never double it.
        expired = (
            await conn.execute(
                text("""
                    WITH expired AS (
                      SELECT id FROM reservations
                       WHERE status = 'HELD' AND expires_at <= now()
                       ORDER BY expires_at
                       LIMIT :batch
                       FOR UPDATE SKIP LOCKED
                    )
                    UPDATE reservations r SET status = 'EXPIRED', updated_at = now()
                      FROM expired WHERE r.id = expired.id
                    RETURNING r.product_id, r.qty
                """),
                {"batch": batch_size},
            )
        ).all()

        returned: dict[int, int] = defaultdict(int)
        for product_id, qty in expired:
            returned[product_id] += qty

        # Sorted order: two reapers touching the same products always lock them in the
        # same order, so they can't deadlock each other.
        for product_id in sorted(returned):
            await conn.execute(
                text("""
                    UPDATE products
                       SET available = available + :qty,
                           version = version + 1,
                           updated_at = now()
                     WHERE id = :pid
                """),
                {"qty": returned[product_id], "pid": product_id},
            )
    return len(expired)


async def reap_once(engine: AsyncEngine, batch_size: int) -> int:
    """One reaper run: batches until a batch comes back short. Returns holds expired."""
    total = 0
    while True:
        count = await _reap_batch(engine, batch_size)
        total += count
        if count < batch_size:
            return total


async def run_reaper(engine: AsyncEngine, interval_seconds: float, batch_size: int) -> None:
    """Loop forever (until cancelled). A failed run is logged and retried next interval."""
    log.info("reaper: started (every %ss, batch %s)", interval_seconds, batch_size)
    while True:
        try:
            expired = await reap_once(engine, batch_size)
            if expired:
                log.info("reaper: expired %d hold(s)", expired)
        except Exception:
            # Never let one bad run (DB blip, etc.) kill the loop.
            # CancelledError is not an Exception subclass, so shutdown still stops us.
            log.exception("reaper: run failed; retrying in %ss", interval_seconds)
        await asyncio.sleep(interval_seconds)
