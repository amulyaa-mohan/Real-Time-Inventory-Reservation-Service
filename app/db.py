from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import settings


def create_engine() -> AsyncEngine:
    # One connection pool per process; pool_pre_ping drops dead connections
    # (e.g. after a Postgres restart) instead of failing the next request.
    return create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        pool_pre_ping=True,
    )


async def check_db(engine: AsyncEngine) -> None:
    """Raises if the database is unreachable."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
