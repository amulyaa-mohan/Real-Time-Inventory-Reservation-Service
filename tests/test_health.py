"""/health and startup config checks."""

import asyncio
import time

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.routes import health


async def test_health_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"db": "ok", "redis": "ok"}


async def test_health_hung_db_returns_503_promptly(client, monkeypatch):
    async def hung_db(engine):
        await asyncio.sleep(60)

    monkeypatch.setattr(health, "check_db", hung_db)
    monkeypatch.setattr(health, "DB_HEALTH_TIMEOUT_SECONDS", 0.2)

    start = time.perf_counter()
    resp = await client.get("/health")

    assert resp.status_code == 503
    assert resp.json() == {"db": "error", "redis": "ok"}
    assert time.perf_counter() - start < 2


@pytest.mark.parametrize(
    "field, value",
    [
        ("REAPER_BATCH_SIZE", 0),  # would make reap_once() loop forever
        ("REAPER_INTERVAL_SECONDS", 0),
        ("HOLD_TTL_SECONDS", -1),
        ("IDEMPOTENCY_TTL_SECONDS", 0),
        ("MAX_RESERVE_RETRIES", 0),  # every reserve would be 503
        ("DB_POOL_SIZE", 0),
    ],
)
def test_settings_reject_bad_values(monkeypatch, field, value):
    monkeypatch.setenv(field, str(value))
    with pytest.raises(ValidationError):
        Settings()
