"""Create the hot SKU for the load test: HOT-1 with total_qty=50. Prints its id.

Re-runnable: any previous HOT-1 (and its reservations) is deleted first, so every run
starts from exactly 50 available. Only HOT-1 rows are touched.

    python loadtest/seed.py

Env: API_URL (default http://localhost:8000),
     LOADTEST_DATABASE_URL (default postgresql://inv:inv@localhost:5432/inventory)
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
import httpx

API_URL = os.environ.get("API_URL", "http://localhost:8000")
DATABASE_URL = os.environ.get("LOADTEST_DATABASE_URL", "postgresql://inv:inv@localhost:5432/inventory")
SKU = "HOT-1"
TOTAL_QTY = 50
ID_FILE = Path(__file__).with_name("hot_product.json")  # read by locustfile.py / verify.py


async def delete_previous_hot_sku() -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM reservations WHERE product_id IN (SELECT id FROM products WHERE sku = $1)", SKU
            )
            await conn.execute("DELETE FROM products WHERE sku = $1", SKU)
    finally:
        await conn.close()


def main() -> int:
    asyncio.run(delete_previous_hot_sku())

    resp = httpx.post(f"{API_URL}/products", json={"sku": SKU, "name": "Hot item", "total_qty": TOTAL_QTY})
    if resp.status_code != 201:
        print(f"seed failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return 1

    product = resp.json()
    ID_FILE.write_text(json.dumps({"product_id": product["id"], "sku": SKU, "total_qty": TOTAL_QTY}))
    print(product["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
