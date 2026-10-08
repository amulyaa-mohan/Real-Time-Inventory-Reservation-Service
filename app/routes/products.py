from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_engine
from app.errors import AppError, not_found
from app.models import ProductCreate, ProductOut

router = APIRouter(prefix="/products", tags=["products"])

# BIGSERIAL upper bound; larger ids are rejected with 422 before hitting the DB.
ProductId = Annotated[int, Path(ge=1, le=2**63 - 1)]

PRODUCT_COLUMNS = "id, sku, name, total_qty, available, version, updated_at"


@router.post("", status_code=201, response_model=ProductOut)
async def create_product(body: ProductCreate, engine: AsyncEngine = Depends(get_engine)):
    # New products start fully available. ON CONFLICT DO NOTHING returns no row
    # when the SKU already exists, so a duplicate becomes a clean 409 (no exception).
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(f"""
                    INSERT INTO products (sku, name, total_qty, available)
                    VALUES (:sku, :name, :qty, :qty)
                    ON CONFLICT (sku) DO NOTHING
                    RETURNING {PRODUCT_COLUMNS}
                """),
                {"sku": body.sku, "name": body.name, "qty": body.total_qty},
            )
        ).mappings().first()
    if row is None:
        raise AppError(409, "SKU_EXISTS", f"product with sku '{body.sku}' already exists")
    return row


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(product_id: ProductId, engine: AsyncEngine = Depends(get_engine)):
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(f"SELECT {PRODUCT_COLUMNS} FROM products WHERE id = :id"),
                {"id": product_id},
            )
        ).mappings().first()
    if row is None:
        raise not_found("product")
    return row
