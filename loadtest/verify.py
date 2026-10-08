"""Check the stock invariant after the load test. Exits non-zero on failure.

Checks, for the hot SKU (and every product via /admin/invariants):
  - SUM(qty) of HELD + CONFIRMED reservations <= total_qty (50): never oversold
  - available >= 0
  - available + SUM(qty HELD/CONFIRMED) == total_qty
  - if Locust's outcome file exists: the number of 201s == reservations in the DB

    python loadtest/verify.py
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
HERE = Path(__file__).parent


async def query_db(product_id: int) -> dict:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        product = await conn.fetchrow("SELECT total_qty, available FROM products WHERE id = $1", product_id)
        by_status = dict(
            await conn.fetch(
                "SELECT status, COALESCE(SUM(qty), 0) FROM reservations WHERE product_id = $1 GROUP BY status",
                product_id,
            )
        )
        rows = await conn.fetchval("SELECT COUNT(*) FROM reservations WHERE product_id = $1", product_id)
    finally:
        await conn.close()
    return {"total_qty": product["total_qty"], "available": product["available"], "by_status": by_status, "rows": rows}


def main() -> int:
    hot = json.loads((HERE / "hot_product.json").read_text())
    product_id = hot["product_id"]
    failures = []

    # 1. The API's own invariant report (all products, one consistent snapshot).
    report = httpx.get(f"{API_URL}/admin/invariants").json()
    if not report["ok"]:
        failures.append(f"/admin/invariants reports a violation: {[p for p in report['products'] if not p['ok']]}")

    # 2. Direct DB checks for the hot SKU.
    db = asyncio.run(query_db(product_id))
    reserved = db["by_status"].get("HELD", 0) + db["by_status"].get("CONFIRMED", 0)
    print(f"HOT-1 (id {product_id}): total_qty={db['total_qty']} available={db['available']} "
          f"reserved(HELD+CONFIRMED)={reserved} reservation rows={db['rows']} by status={db['by_status']}")

    if reserved > db["total_qty"]:
        failures.append(f"OVERSOLD: {reserved} reserved > {db['total_qty']} total")
    if db["available"] < 0:
        failures.append(f"available is negative: {db['available']}")
    if db["available"] + reserved != db["total_qty"]:
        failures.append(f"invariant broken: {db['available']} + {reserved} != {db['total_qty']}")

    # 3. Cross-check the client's view (Locust) against the DB, if a run produced it.
    outcomes_file = HERE / "results_status_counts.json"
    if outcomes_file.exists():
        outcomes = json.loads(outcomes_file.read_text())
        if outcomes.get("product_id") == product_id:
            print(f"Locust outcomes: {outcomes['status_counts']}")
            if outcomes["reserved_201"] != db["rows"]:
                failures.append(f"Locust saw {outcomes['reserved_201']} x 201 but DB has {db['rows']} reservations")
            if outcomes["unexpected"]:
                failures.append(f"{outcomes['unexpected']} unexpected responses during the load test")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("INVARIANT OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
