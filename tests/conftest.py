"""Shared fixtures.

Tests run against the real Postgres + Redis from docker compose (localhost ports),
using the same database as the dev API. Every test starts from empty tables
and an empty Redis DB.
"""

import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text

from app.main import app, lifespan


@pytest.fixture
async def engine():
    # Run the real startup/shutdown (creates the pool, Redis client, schema).
    # ASGITransport does not trigger the lifespan by itself, so we enter it here.
    async with lifespan(app):
        async with app.state.engine.begin() as conn:
            await conn.execute(text("TRUNCATE reservations, products RESTART IDENTITY CASCADE"))
        # Idempotency cache entries would otherwise outlive the truncated rows.
        await app.state.redis.flushdb()
        yield app.state.engine


@pytest.fixture
async def client(engine):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def stock_snapshot(engine, product_id: int) -> dict:
    """available, total_qty and the qty currently held/confirmed for one product."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("""
                    SELECT p.total_qty, p.available, p.version,
                           COALESCE((SELECT SUM(r.qty) FROM reservations r
                                      WHERE r.product_id = p.id
                                        AND r.status IN ('HELD', 'CONFIRMED')), 0) AS reserved
                      FROM products p
                     WHERE p.id = :id
                """),
                {"id": product_id},
            )
        ).mappings().one()
    return dict(row)


async def assert_invariant(engine, product_id: int) -> dict:
    """BUILDBOOK section 5: available + SUM(qty HELD/CONFIRMED) == total_qty."""
    snap = await stock_snapshot(engine, product_id)
    assert snap["available"] >= 0, snap
    assert snap["available"] + snap["reserved"] == snap["total_qty"], snap
    return snap


async def insert_hold(
    engine, product_id: int, qty: int = 1, status: str = "HELD", expires_in_seconds: float = 120
) -> str:
    """Insert a reservation directly in the DB (e.g. with a past expires_at), keeping the
    stock invariant: HELD/CONFIRMED take stock from `available`, RELEASED/EXPIRED don't."""
    reservation_id = uuid.uuid4()
    async with engine.begin() as conn:
        if status in ("HELD", "CONFIRMED"):
            await conn.execute(
                text("UPDATE products SET available = available - :qty, version = version + 1 WHERE id = :id"),
                {"qty": qty, "id": product_id},
            )
        await conn.execute(
            text("""
                INSERT INTO reservations (id, product_id, qty, status, idempotency_key, request_hash, expires_at)
                VALUES (:id, :pid, :qty, :status, :key, 'test', now() + CAST(:ttl AS interval))
            """),
            {
                "id": reservation_id,
                "pid": product_id,
                "qty": qty,
                "status": status,
                "key": f"test-{reservation_id}",
                "ttl": timedelta(seconds=expires_in_seconds),
            },
        )
    return str(reservation_id)


@pytest.fixture
def make_product(client):
    """Create a product through the API and return its JSON."""

    async def _make(sku: str = "SKU-1", name: str = "Widget", total_qty: int = 10) -> dict:
        resp = await client.post("/products", json={"sku": sku, "name": name, "total_qty": total_qty})
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _make
