import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI

from app.config import settings
from app.db import create_engine, init_schema
from app.errors import register_error_handlers
from app.routes import health, products, reservations
from app.services.reaper import run_reaper

# Libraries log at WARNING+; our own app.* loggers (reaper, retries) also at INFO.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("app").setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: open the DB pool and Redis client once, share them via app.state.
    # Shutdown closes these local references, so anything that reassigns app.state
    # in the meantime can't make us close the wrong objects (or leak these).
    engine = create_engine()
    # Short timeouts: Redis is only a cache, so a hung Redis must not stall requests.
    redis_client = redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    app.state.engine = engine
    app.state.redis = redis_client
    reaper_task = None
    try:
        await init_schema(engine)
        # Background expiry loop (BUILDBOOK 7.4).
        reaper_task = asyncio.create_task(
            run_reaper(engine, settings.REAPER_INTERVAL_SECONDS, settings.REAPER_BATCH_SIZE),
            name="reaper",
        )
        yield
    finally:
        # Shutdown: stop the reaper first (it uses the pool), then close connections.
        if reaper_task is not None:
            reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper_task
        await redis_client.aclose()
        await engine.dispose()


app = FastAPI(title="Inventory Reservation Service", lifespan=lifespan)
register_error_handlers(app)
app.include_router(health.router)
app.include_router(products.router)
app.include_router(reservations.router)
