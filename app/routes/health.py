import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import check_db, get_engine

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    status = {"db": "ok", "redis": "ok"}

    try:
        await check_db(request.app.state.engine)
    except Exception:
        log.exception("health: database check failed")
        status["db"] = "error"

    try:
        await request.app.state.redis.ping()
    except Exception:
        log.exception("health: redis check failed")
        status["redis"] = "error"

    ok = all(v == "ok" for v in status.values())
    return JSONResponse(status, status_code=200 if ok else 503)


@router.get("/admin/invariants")
async def invariants(engine: AsyncEngine = Depends(get_engine)) -> dict:
    """Per product: available + SUM(qty HELD/CONFIRMED) == total_qty, and 0 <= available.

    One SQL statement, so every product is read from a single consistent snapshot; two
    separate queries could see a reserve half-way and report a false violation.
    """
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("""
                    SELECT p.id, p.sku, p.total_qty, p.available,
                           COALESCE(SUM(r.qty) FILTER (WHERE r.status IN ('HELD', 'CONFIRMED')), 0) AS reserved
                      FROM products p
                      LEFT JOIN reservations r ON r.product_id = p.id
                     GROUP BY p.id
                     ORDER BY p.id
                """)
            )
        ).mappings().all()

    products = []
    for row in rows:
        ok = row["available"] >= 0 and row["available"] + row["reserved"] == row["total_qty"]
        products.append({**row, "ok": ok})
    return {"ok": all(p["ok"] for p in products), "products": products}
