"""Phase 2 headline test: concurrent reserves on one SKU must never oversell."""

import asyncio
import uuid
from collections import Counter

from tests.conftest import assert_invariant

STOCK = 10
REQUESTS = 100


async def test_100_concurrent_reserves_never_oversell(client, engine, make_product):
    product = await make_product(sku="HOT", total_qty=STOCK)

    async def one_reserve():
        return await client.post(
            "/reservations",
            json={"product_id": product["id"], "qty": 1},
            headers={"Idempotency-Key": str(uuid.uuid4())},  # unique key per request
        )

    responses = await asyncio.gather(*(one_reserve() for _ in range(REQUESTS)))
    statuses = Counter(r.status_code for r in responses)
    print(f"\nstatus counts: {dict(statuses)}")

    # Exactly the stock was sold; everyone else was turned away (409 sold out / 503 busy).
    assert statuses[201] == STOCK
    assert statuses[409] + statuses[503] == REQUESTS - STOCK
    assert set(statuses) <= {201, 409, 503}

    # Every winner got a distinct reservation.
    winners = [r.json()["id"] for r in responses if r.status_code == 201]
    assert len(set(winners)) == STOCK

    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 0
    assert snap["reserved"] == STOCK
