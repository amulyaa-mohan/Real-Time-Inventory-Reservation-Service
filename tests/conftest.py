"""Shared fixtures.

Tests run against the real Postgres + Redis from docker compose (localhost ports), but
in their OWN database (inventory_test) and Redis DB index (1). The running API container
uses `inventory` / DB 0, so its reaper can't touch test rows and tests can't wipe dev
data. Every test starts from empty tables and an empty Redis DB.
"""

import asyncio
import os
import uuid
from datetime import timedelta

# Must be set before app.config is imported (Settings reads env at import time).
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://inv:inv@localhost:5432/inventory_test"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.main as main_module  # noqa: E402
from app.main import app, lifespan  # noqa: E402


def pytest_sessionstart(session):
    """Create the test database once if it doesn't exist (needs the compose Postgres)."""

    async def ensure_db():
        dsn = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        base, db_name = dsn.rsplit("/", 1)
        conn = await asyncpg.connect(f"{base}/postgres")
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{db_name}"')
        finally:
            await conn.close()

    asyncio.run(ensure_db())


async def _idle_reaper(*args, **kwargs):
    """Stand-in for the background loop: waits until cancelled, never reaps."""
    await asyncio.Event().wait()


@pytest.fixture
async def engine(monkeypatch):
    # Run the real startup/shutdown (creates the pool, Redis client, schema).
    # ASGITransport does not trigger the lifespan by itself, so we enter it here.
    # The background loop is swapped for an idle task so it can't race tests;
    # reaper tests call reap_once() directly.
    monkeypatch.setattr(main_module, "run_reaper", _idle_reaper)
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
