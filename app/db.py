from pathlib import Path

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def create_engine() -> AsyncEngine:
    # One connection pool per process; pool_pre_ping drops dead connections
    # (e.g. after a Postgres restart) instead of failing the next request.
    return create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        pool_pre_ping=True,
    )


def _schema_statements() -> list[str]:
    # asyncpg runs one statement per execute, so the file is split on ';'.
    # Strip '--' comments first so a ';' inside a comment can't split a statement.
    # (schema.sql must not contain '--' or ';' inside string literals.)
    lines = [ln.split("--", 1)[0] for ln in SCHEMA_PATH.read_text(encoding="utf-8").splitlines()]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


# Arbitrary app-wide constant identifying the "schema init" advisory lock.
SCHEMA_LOCK_ID = 727_001


async def init_schema(engine: AsyncEngine) -> None:
    """Create tables/indexes if missing. Safe to run on every startup, from many replicas."""
    async with engine.begin() as conn:
        # CREATE TABLE IF NOT EXISTS is not safe to run concurrently (two sessions can
        # both pass the check and one fails on pg_type). This transaction-scoped lock
        # makes replicas that start together take turns; it's released at COMMIT.
        await conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": SCHEMA_LOCK_ID})
        for stmt in _schema_statements():
            await conn.execute(text(stmt))


async def check_db(engine: AsyncEngine) -> None:
    """Raises if the database is unreachable."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


def get_engine(request: Request) -> AsyncEngine:
    """FastAPI dependency: the process-wide engine created in the lifespan."""
    return request.app.state.engine
