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


@pytest.fixture
def make_product(client):
    """Create a product through the API and return its JSON."""

    async def _make(sku: str = "SKU-1", name: str = "Widget", total_qty: int = 10) -> dict:
        resp = await client.post("/products", json={"sku": sku, "name": name, "total_qty": total_qty})
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _make
