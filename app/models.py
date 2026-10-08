"""Pydantic request/response schemas (API shapes only; tables live in schema.sql)."""

from datetime import datetime

from pydantic import BaseModel, Field

# Postgres INTEGER is 32-bit; reject larger values with a 422 instead of a DB error.
PG_INT_MAX = 2**31 - 1


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    total_qty: int = Field(ge=0, le=PG_INT_MAX)


class ProductOut(BaseModel):
    id: int
    sku: str
    name: str
    total_qty: int
    available: int
    version: int
    updated_at: datetime
