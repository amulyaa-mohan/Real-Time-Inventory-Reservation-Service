import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

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
