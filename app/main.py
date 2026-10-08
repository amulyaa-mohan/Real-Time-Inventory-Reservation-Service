from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI

from app.config import settings
from app.db import create_engine
from app.routes import health


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: open the DB pool and Redis client once, share them via app.state.
    app.state.engine = create_engine()
    app.state.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        yield
    finally:
        # Shutdown: close connections cleanly.
        await app.state.redis.aclose()
        await app.state.engine.dispose()


app = FastAPI(title="Inventory Reservation Service", lifespan=lifespan)
app.include_router(health.router)
