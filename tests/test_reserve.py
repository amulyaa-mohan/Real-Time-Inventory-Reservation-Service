import uuid
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from tests.conftest import assert_invariant

# ---------- Products (Phase 1) ----------


async def test_create_product_sets_available_to_total(client):
    resp = await client.post("/products", json={"sku": "SKU-1", "name": "Widget", "total_qty": 10})
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] >= 1
    assert body["sku"] == "SKU-1"
    assert body["total_qty"] == 10
    assert body["available"] == 10
    assert body["version"] == 0


async def test_get_product_round_trip(client, make_product):
    created = await make_product(total_qty=7)
    resp = await client.get(f"/products/{created['id']}")
    assert resp.status_code == 200
    assert resp.json() == created


async def test_get_product_not_found(client):
    resp = await client.get("/products/999999")
    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "NOT_FOUND", "message": "product not found"}}


async def test_create_product_duplicate_sku_conflicts(client, make_product):
    await make_product(sku="DUP")
    resp = await client.post("/products", json={"sku": "DUP", "name": "Other", "total_qty": 1})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SKU_EXISTS"


@pytest.mark.parametrize(
    "payload",
    [
        {"sku": "X", "name": "Bad", "total_qty": -1},  # negative stock
        {"sku": "", "name": "Bad", "total_qty": 1},  # empty sku
        {"name": "Bad", "total_qty": 1},  # missing sku
    ],
)
async def test_create_product_invalid_body(client, payload):
    resp = await client.post("/products", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_product_available_check_constraint_blocks_negative_stock(engine, make_product):
    # Defence in depth: even if app logic were wrong, Postgres refuses available < 0.
    product = await make_product(total_qty=1)
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE products SET available = -1 WHERE id = :id"), {"id": product["id"]}
            )


# ---------- Reserve (Phase 2) ----------


async def reserve(client, product_id: int, qty: int = 1, key: str | None = None):
    headers = {"Idempotency-Key": key or str(uuid.uuid4())}
    return await client.post("/reservations", json={"product_id": product_id, "qty": qty}, headers=headers)


async def test_reserve_happy_path(client, engine, make_product):
    product = await make_product(total_qty=10)

    resp = await reserve(client, product["id"], qty=3)

    assert resp.status_code == 201
    assert "Idempotent-Replayed" not in resp.headers
    body = resp.json()
    assert body["product_id"] == product["id"]
    assert body["qty"] == 3
    assert body["status"] == "HELD"
    hold = datetime.fromisoformat(body["expires_at"]) - datetime.fromisoformat(body["created_at"])
    assert abs(hold.total_seconds() - settings.HOLD_TTL_SECONDS) < 1

    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 7
    assert snap["version"] == 1

    # And it can be read back.
    got = await client.get(f"/reservations/{body['id']}")
    assert got.status_code == 200
    assert got.json() == body


async def test_reserve_exact_remaining_stock_then_out_of_stock(client, engine, make_product):
    product = await make_product(total_qty=2)

    assert (await reserve(client, product["id"], qty=2)).status_code == 201

    resp = await reserve(client, product["id"], qty=1)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "OUT_OF_STOCK"
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 0


async def test_reserve_more_than_available_changes_nothing(client, engine, make_product):
    product = await make_product(total_qty=2)

    resp = await reserve(client, product["id"], qty=3)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "OUT_OF_STOCK"
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 2
    assert snap["version"] == 0  # the UPDATE never ran
    assert snap["reserved"] == 0  # no reservation row


async def test_reserve_unknown_product_404(client):
    resp = await reserve(client, 999999)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": ""}, {"Idempotency-Key": "   "}])
async def test_reserve_missing_idempotency_key_400(client, make_product, headers):
    product = await make_product()
    resp = await client.post("/reservations", json={"product_id": product["id"], "qty": 1}, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


@pytest.mark.parametrize("qty", [0, -1])
async def test_reserve_non_positive_qty_422(client, make_product, qty):
    product = await make_product()
    resp = await reserve(client, product["id"], qty=qty)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_reserve_same_key_replays_from_db(client, engine, make_product):
    # Step 2 of 7.1 (DB lookup). Full idempotency coverage is in test_idempotency.py (Phase 3).
    product = await make_product(total_qty=5)

    first = await reserve(client, product["id"], qty=2, key="key-1")
    second = await reserve(client, product["id"], qty=2, key="key-1")

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 3  # decremented once


async def test_reserve_retries_exhausted_503(client, engine, make_product, monkeypatch):
    # Force the "every attempt lost the version race" outcome without timing tricks.
    product = await make_product(total_qty=5)
    monkeypatch.setattr(settings, "MAX_RESERVE_RETRIES", 0)

    resp = await reserve(client, product["id"])

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "1"
    assert resp.json()["error"]["code"] == "BUSY"
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 5


async def test_get_reservation_unknown_404(client):
    resp = await client.get(f"/reservations/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_reserve_overlong_idempotency_key_400(client, make_product):
    # Regression: a >2.7KB key used to hit Postgres' btree row limit and return 500.
    product = await make_product()
    resp = await reserve(client, product["id"], key="k" * 256)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_IDEMPOTENCY_KEY"
    assert (await reserve(client, product["id"], key="k" * 255)).status_code == 201


@pytest.mark.parametrize("qty", ["1", 1.0, True])
async def test_reserve_non_integer_qty_422(client, make_product, qty):
    product = await make_product()
    resp = await reserve(client, product["id"], qty=qty)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
