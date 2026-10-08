"""Phase 5: reaper (BUILDBOOK 7.4) and GET /admin/invariants."""

import asyncio
import logging

from sqlalchemy import text

from app.config import settings
import app.main as main_module
from app.main import app, lifespan
from app.services import reaper
from app.services.reaper import reap_once, run_reaper
from tests.conftest import assert_invariant, insert_hold

BATCH = 500


async def statuses(engine, ids: list[str]) -> list[str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT id::text, status FROM reservations WHERE id::text = ANY(:ids)"), {"ids": ids})
        ).all()
    by_id = dict(rows)
    return [by_id[i] for i in ids]


async def warm_pool(engine, n: int = 20) -> None:
    """Open n pooled connections up front. A burst that has to open new connections
    pays ~1s of connect latency (Docker on Windows), which would skew timing races."""

    async def hold_one():
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            await asyncio.sleep(0.05)

    await asyncio.gather(*(hold_one() for _ in range(n)))


async def test_reaper_expires_overdue_holds_only(engine, make_product):
    # BUILDBOOK Verify: 3 expired HELD holds + 1 live one, one direct reaper call.
    product = await make_product(total_qty=20)
    expired = [
        await insert_hold(engine, product["id"], qty=q, expires_in_seconds=-60) for q in (1, 2, 3)
    ]
    live = await insert_hold(engine, product["id"], qty=4, expires_in_seconds=600)
    assert (await assert_invariant(engine, product["id"]))["available"] == 10

    count = await reap_once(engine, BATCH)

    assert count == 3
    assert await statuses(engine, expired) == ["EXPIRED"] * 3
    assert await statuses(engine, [live]) == ["HELD"]
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 16  # 10 + (1 + 2 + 3) returned; the live hold's 4 still held
    assert snap["version"] == 5  # 4 inserts + 1 reaper update (one UPDATE per product per batch)


async def test_reaper_returns_stock_per_product(engine, make_product):
    a = await make_product(sku="A", total_qty=10)
    b = await make_product(sku="B", total_qty=10)
    for q in (1, 2):
        await insert_hold(engine, a["id"], qty=q, expires_in_seconds=-1)
    await insert_hold(engine, b["id"], qty=5, expires_in_seconds=-1)

    assert await reap_once(engine, BATCH) == 3

    assert (await assert_invariant(engine, a["id"]))["available"] == 10
    assert (await assert_invariant(engine, b["id"]))["available"] == 10


async def test_reaper_ignores_non_held_rows(engine, make_product):
    product = await make_product(total_qty=10)
    ids = [
        await insert_hold(engine, product["id"], qty=1, status=s, expires_in_seconds=-60)
        for s in ("CONFIRMED", "RELEASED", "EXPIRED")
    ]

    assert await reap_once(engine, BATCH) == 0

    assert await statuses(engine, ids) == ["CONFIRMED", "RELEASED", "EXPIRED"]
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 9  # only the CONFIRMED unit is taken; nothing returned


async def test_reaper_loops_over_batches(engine, make_product):
    product = await make_product(total_qty=10)
    for _ in range(5):
        await insert_hold(engine, product["id"], qty=1, expires_in_seconds=-1)

    assert await reap_once(engine, batch_size=2) == 5  # batches of 2, 2, 1

    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 10


async def test_reaper_second_run_is_noop(engine, make_product):
    product = await make_product(total_qty=10)
    await insert_hold(engine, product["id"], qty=3, expires_in_seconds=-1)
    assert await reap_once(engine, BATCH) == 1

    assert await reap_once(engine, BATCH) == 0

    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 10  # not 13


async def test_two_reapers_concurrently_return_stock_once(engine, make_product):
    # SKIP LOCKED: concurrent reapers (e.g. two API replicas) split the rows, never double-count.
    product = await make_product(total_qty=200)
    for _ in range(200):
        await insert_hold(engine, product["id"], qty=1, expires_in_seconds=-1)

    counts = await asyncio.gather(*(reap_once(engine, batch_size=10) for _ in range(4)))

    print(f"\nexpired per reaper: {counts}")
    assert sum(counts) == 200
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 200


async def test_reaper_vs_release_race_returns_stock_once(client, engine, make_product):
    product = await make_product(total_qty=20)
    ids = [await insert_hold(engine, product["id"], qty=1, expires_in_seconds=-1) for _ in range(20)]

    results = await asyncio.gather(
        reap_once(engine, batch_size=5),
        *(client.delete(f"/reservations/{rid}") for rid in ids),
    )

    reaped, releases = results[0], results[1:]
    released = sum(r.status_code == 200 for r in releases)
    print(f"\nreaped={reaped} released={released}")
    # Each hold went to exactly one of EXPIRED or RELEASED; the losing release got 409.
    assert reaped + released == 20
    assert all(r.status_code in (200, 409) for r in releases)
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 20


async def test_reaper_vs_confirm_race_exactly_one_wins(client, engine, make_product):
    # Holds expire *during* the burst of confirms while a reaper polls every few ms, so
    # some confirms land before expiry and some after. Confirm (expires_at > now()) and
    # the reaper (expires_at <= now()) are both conditional on status='HELD'; the row
    # lock (and SKIP LOCKED) guarantee each hold ends in exactly one of the two states.
    product = await make_product(total_qty=40)
    ids = [await insert_hold(engine, product["id"], qty=1, expires_in_seconds=600) for _ in range(40)]
    await warm_pool(engine)  # otherwise opening fresh connections takes longer than the TTL
    async with engine.begin() as conn:  # one statement, so all 40 expire ~120ms from now
        await conn.execute(text("UPDATE reservations SET expires_at = now() + interval '120 milliseconds'"))

    reaped = 0

    async def reaper_polling():
        nonlocal reaped
        for _ in range(60):
            reaped += await reap_once(engine, batch_size=5)
            await asyncio.sleep(0.005)

    async def staggered_confirm(i, rid):
        await asyncio.sleep(i * 0.006)  # spread confirms across the expiry moment
        return await client.post(f"/reservations/{rid}/confirm")

    results = await asyncio.gather(reaper_polling(), *(staggered_confirm(i, rid) for i, rid in enumerate(ids)))
    confirms = results[1:]

    final = await statuses(engine, ids)
    codes = [r.status_code for r in confirms]
    print(f"\nconfirmed={codes.count(200)} expired(410)={codes.count(410)} reaped={reaped}")
    assert set(codes) <= {200, 410}
    assert set(final) <= {"CONFIRMED", "EXPIRED"}
    assert codes.count(200) == final.count("CONFIRMED")
    assert reaped == final.count("EXPIRED")
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == final.count("EXPIRED")


async def test_reaper_loop_survives_errors(engine, monkeypatch, caplog):
    calls = 0

    async def flaky_reap_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db blip (test)")
        return 0

    monkeypatch.setattr(reaper, "reap_once", flaky_reap_once)
    with caplog.at_level(logging.ERROR, logger="app.services.reaper"):
        task = asyncio.create_task(run_reaper(engine, interval_seconds=0.01, batch_size=BATCH))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert calls >= 3  # kept going after the failure
    assert any("reaper: run failed" in r.message for r in caplog.records)
    assert task.cancelled()


async def test_lifespan_runs_reaper_in_background_and_stops_it(engine, make_product, monkeypatch):
    # The real wiring: lifespan starts the loop, it expires holds on its own, shutdown cancels it.
    product = await make_product(total_qty=10)
    rid = await insert_hold(engine, product["id"], qty=3, expires_in_seconds=-1)
    tasks_before = {t for t in asyncio.all_tasks() if t.get_name() == "reaper"}

    monkeypatch.setattr(main_module, "run_reaper", run_reaper)  # the fixture installed an idle stub
    monkeypatch.setattr(settings, "REAPER_INTERVAL_SECONDS", 0.05)
    async with lifespan(app):
        reaper_tasks = {t for t in asyncio.all_tasks() if t.get_name() == "reaper"} - tasks_before
        assert len(reaper_tasks) == 1
        for _ in range(40):  # up to 2s
            if (await statuses(engine, [rid])) == ["EXPIRED"]:
                break
            await asyncio.sleep(0.05)
    (task,) = reaper_tasks

    assert await statuses(engine, [rid]) == ["EXPIRED"]
    assert task.done() and task.cancelled()
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 10


# ---------- /admin/invariants ----------


async def test_admin_invariants_ok(client, engine, make_product):
    a = await make_product(sku="A", total_qty=10)
    b = await make_product(sku="B", total_qty=5)
    await insert_hold(engine, a["id"], qty=3)
    await insert_hold(engine, a["id"], qty=2, status="CONFIRMED")
    await insert_hold(engine, b["id"], qty=1, status="RELEASED")

    resp = await client.get("/admin/invariants")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["products"] == [
        {"id": a["id"], "sku": "A", "total_qty": 10, "available": 5, "reserved": 5, "ok": True},
        {"id": b["id"], "sku": "B", "total_qty": 5, "available": 5, "reserved": 0, "ok": True},
    ]


async def test_admin_invariants_detects_violation(client, engine, make_product):
    good = await make_product(sku="GOOD", total_qty=10)
    bad = await make_product(sku="BAD", total_qty=10)
    async with engine.begin() as conn:  # simulate a bug: stock vanished without a reservation
        await conn.execute(text("UPDATE products SET available = 7 WHERE id = :id"), {"id": bad["id"]})

    body = (await client.get("/admin/invariants")).json()

    assert body["ok"] is False
    by_sku = {p["sku"]: p for p in body["products"]}
    assert by_sku["GOOD"]["ok"] is True
    assert by_sku["BAD"]["ok"] is False


async def test_admin_invariants_empty(client):
    body = (await client.get("/admin/invariants")).json()
    assert body == {"ok": True, "products": []}
