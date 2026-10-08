"""Pydantic request/response schemas (API shapes only; tables live in schema.sql)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# Postgres INTEGER is 32-bit and BIGINT is 64-bit; reject larger values with a 422
# instead of letting them fail inside the DB driver as a 500.
PG_INT_MAX = 2**31 - 1
PG_BIGINT_MAX = 2**63 - 1


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    total_qty: int = Field(strict=True, ge=0, le=PG_INT_MAX)


class ProductOut(BaseModel):
    id: int
    sku: str
    name: str
    total_qty: int
    available: int
    version: int
    updated_at: datetime


class ReservationCreate(BaseModel):
    # strict: JSON must send a real integer; "2", 2.0 and true are rejected (422).
    product_id: int = Field(strict=True, ge=1, le=PG_BIGINT_MAX)
    qty: int = Field(strict=True, ge=1, le=PG_INT_MAX)


class ReservationOut(BaseModel):
    id: UUID
    product_id: int
    qty: int
    status: Literal["HELD", "CONFIRMED", "RELEASED", "EXPIRED"]
    expires_at: datetime
    created_at: datetime
