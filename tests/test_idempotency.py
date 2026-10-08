"""Phase 3: Idempotency-Key semantics, Redis fast path, and Redis-down fallback."""

import asyncio
import json
import logging
from collections import Counter

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text

from app.main import app
from app.services import reservations as svc
from tests.conftest import assert_invariant


async def reserve(client, product_id: int, qty: int, key: str):
    return await client.post(
        "/reservations",
        json={"product_id": product_id, "qty": qty},
        headers={"Idempotency-Key": key},
    )


async def count_reservations(engine, key: str) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT COUNT(*) FROM reservations WHERE idempotency_key = :k"), {"k": key}
            )
        ).scalar_one()


async def test_same_key_same_body_replays_and_decrements_once(client, engine, make_product):
    product = await make_product(total_qty=10)

    first = await reserve(client, product["id"], 2, "order-1")
    second = await reserve(client, product["id"], 2, "order-1")

    assert first.status_code == 201
    assert "Idempotent-Replayed" not in first.headers
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()  # same reservation id, identical body
    assert await count_reservations(engine, "order-1") == 1
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 8  # decremented once


async def test_success_is_cached_in_redis_and_replay_skips_db(client, engine, make_product, monkeypatch):
    product = await make_product(total_qty=10)
    first = await reserve(client, product["id"], 2, "order-1")

    cached = json.loads(await app.state.redis.get("idem:order-1"))
    assert cached["request_hash"] == svc.request_hash(product["id"], 2)
    assert cached["status_code"] == 201
    assert cached["body"] == first.json()
    assert 0 < await app.state.redis.ttl("idem:order-1") <= 86400

    # Prove the replay is served from Redis: any DB lookup would now blow up.
    async def no_db(*args, **kwargs):
        raise AssertionError("DB lookup should not happen on a Redis hit")

    monkeypatch.setattr(svc, "_find_by_key", no_db)
    second = await reserve(client, product["id"], 2, "order-1")
    assert second.status_code == 200
    assert second.json() == first.json()


async def test_db_replay_repopulates_redis(client, engine, make_product):
    product = await make_product(total_qty=10)
    first = await reserve(client, product["id"], 2, "order-1")
    await app.state.redis.delete("idem:order-1")  # e.g. evicted or Redis restarted

    second = await reserve(client, product["id"], 2, "order-1")

    assert second.status_code == 200
    assert second.json() == first.json()
    assert await app.state.redis.exists("idem:order-1") == 1


@pytest.mark.parametrize("via", ["redis", "db"])
async def test_same_key_different_body_422(client, engine, make_product, via):
    product = await make_product(total_qty=10)
    assert (await reserve(client, product["id"], 2, "order-1")).status_code == 201
    if via == "db":
        await app.state.redis.delete("idem:order-1")  # force the DB path

    resp = await reserve(client, product["id"], 3, "order-1")

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 8  # the bad request changed nothing


async def test_errors_are_not_cached(client, engine, make_product):
    product = await make_product(total_qty=1)

    resp = await reserve(client, product["id"], 2, "order-1")

    assert resp.status_code == 409
    assert await app.state.redis.exists("idem:order-1") == 0


async def test_20_concurrent_same_key_one_reservation(client, engine, make_product):
    product = await make_product(total_qty=10)

    responses = await asyncio.gather(*(reserve(client, product["id"], 2, "dup-key") for _ in range(20)))
    statuses = Counter(r.status_code for r in responses)
    print(f"\nstatus counts: {dict(statuses)}")

    assert statuses == {201: 1, 200: 19}  # one creator, everyone else replays it
    assert len({r.json()["id"] for r in responses}) == 1
    assert await count_reservations(engine, "dup-key") == 1
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 8  # decremented once


class BrokenRedis:
    """Stands in for a Redis that is down: every command raises."""

    async def get(self, *args, **kwargs):
        raise RedisConnectionError("redis is down (test)")

    async def set(self, *args, **kwargs):
        raise RedisConnectionError("redis is down (test)")

    async def aclose(self):
        pass


async def test_redis_down_still_correct_via_db(client, engine, make_product, caplog, monkeypatch):
    product = await make_product(total_qty=10)

    # Scoped patch: the real client is restored before the lifespan shuts down and closes it.
    with monkeypatch.context() as m, caplog.at_level(logging.WARNING, logger="app.idempotency"):
        m.setattr(app.state, "redis", BrokenRedis())
        first = await reserve(client, product["id"], 2, "order-1")
        second = await reserve(client, product["id"], 2, "order-1")
        mismatch = await reserve(client, product["id"], 5, "order-1")
        dupes = await asyncio.gather(*(reserve(client, product["id"], 1, "dup-key") for _ in range(10)))

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()
    assert mismatch.status_code == 422
    assert Counter(r.status_code for r in dupes) == {201: 1, 200: 9}
    assert await count_reservations(engine, "dup-key") == 1

    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 7  # 10 - 2 (order-1) - 1 (dup-key)
    assert any("redis GET failed" in r.message for r in caplog.records)
    assert any("redis SET failed" in r.message for r in caplog.records)


async def test_concurrent_same_key_taking_last_stock_replays_not_409(client, engine, make_product):
    # Regression: duplicates that reach the stock check after the original took the last
    # units used to get 409 OUT_OF_STOCK instead of the replay of their own success.
    product = await make_product(total_qty=2)

    responses = await asyncio.gather(*(reserve(client, product["id"], 2, "last-units") for _ in range(20)))
    statuses = Counter(r.status_code for r in responses)
    print(f"\nstatus counts: {dict(statuses)}")

    assert statuses == {201: 1, 200: 19}
    assert len({r.json()["id"] for r in responses}) == 1
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 0


async def test_busy_replays_if_same_key_committed_meanwhile(client, engine, make_product, monkeypatch):
    # Same race on the 503 path: step 2 misses (original not committed yet), every
    # attempt loses, but by the time we would return BUSY the original has committed.
    product = await make_product(total_qty=10)
    first = await reserve(client, product["id"], 2, "order-1")
    await app.state.redis.delete("idem:order-1")

    real_find = svc._find_by_key
    calls = 0

    async def find_misses_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real_find(*args, **kwargs)

    monkeypatch.setattr(svc, "_find_by_key", find_misses_once)
    monkeypatch.setattr(svc.settings, "MAX_RESERVE_RETRIES", 0)

    resp = await reserve(client, product["id"], 2, "order-1")

    assert resp.status_code == 200
    assert resp.json() == first.json()
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 8


async def test_out_of_stock_without_duplicate_is_still_409(client, engine, make_product):
    product = await make_product(total_qty=1)
    assert (await reserve(client, product["id"], 1, "a")).status_code == 201

    resp = await reserve(client, product["id"], 1, "b")  # different key: a real sell-out

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "OUT_OF_STOCK"


@pytest.mark.parametrize("change", ["confirm", "release", "expire"])
async def test_replay_returns_original_response_via_redis_and_db(client, engine, make_product, change):
    # A replay answers "what happened to my request?": the original 201 body (HELD),
    # identical whether it is served from Redis or from the DB, even after the
    # reservation moved on. The current state is available via GET /reservations/{id}.
    product = await make_product(total_qty=10)
    first = await reserve(client, product["id"], 2, "order-1")
    rid = first.json()["id"]
    if change == "confirm":
        assert (await client.post(f"/reservations/{rid}/confirm")).status_code == 200
    elif change == "release":
        assert (await client.delete(f"/reservations/{rid}")).status_code == 200
    else:
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE reservations SET status = 'EXPIRED' WHERE id = :id"), {"id": rid})

    via_redis = await reserve(client, product["id"], 2, "order-1")
    await app.state.redis.delete("idem:order-1")
    via_db = await reserve(client, product["id"], 2, "order-1")

    assert via_redis.status_code == via_db.status_code == 200
    assert via_redis.json() == via_db.json() == first.json()
    assert via_db.json()["status"] == "HELD"
    current = (await client.get(f"/reservations/{rid}")).json()["status"]
    assert current == {"confirm": "CONFIRMED", "release": "RELEASED", "expire": "EXPIRED"}[change]
