"""Shared fixtures.

Tests run against the real Postgres + Redis from docker compose (localhost ports),
using the same database as the dev API. Every test starts from empty tables.
"""

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


@pytest.fixture
def make_product(client):
    """Create a product through the API and return its JSON."""

    async def _make(sku: str = "SKU-1", name: str = "Widget", total_qty: int = 10) -> dict:
        resp = await client.post("/products", json={"sku": sku, "name": name, "total_qty": total_qty})
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _make
