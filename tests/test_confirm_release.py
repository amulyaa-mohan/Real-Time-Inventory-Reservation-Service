"""Phase 4: confirm / release state machine (BUILDBOOK 5, 6, 7.2, 7.3).

State table under test (rows = current state, columns = action):

                     confirm             release
  HELD (live)        200 CONFIRMED       200 RELEASED, stock returned
  HELD (past expiry) 410 EXPIRED         200 RELEASED, stock returned
  CONFIRMED          200 (idempotent)    409 INVALID_STATE
  RELEASED           409 INVALID_STATE   200 (idempotent), stock not returned twice
  EXPIRED            410 EXPIRED         409 INVALID_STATE
  missing            404                 404
"""

import asyncio
import uuid
from collections import Counter

import pytest

from tests.conftest import assert_invariant, insert_hold

STOCK = 10


async def confirm(client, rid):
    return await client.post(f"/reservations/{rid}/confirm")


async def release(client, rid):
    return await client.delete(f"/reservations/{rid}")


async def reserve(client, product_id, qty=2):
    resp = await client.post(
        "/reservations",
        json={"product_id": product_id, "qty": qty},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def assert_error(resp, status_code, code):
    assert resp.status_code == status_code, resp.text
    assert resp.json()["error"]["code"] == code


# ---------- confirm ----------


async def test_confirm_held(client, engine, make_product):
    product = await make_product(total_qty=STOCK)
    hold = await reserve(client, product["id"], qty=2)

    resp = await confirm(client, hold["id"])

    assert resp.status_code == 200
    assert resp.json()["status"] == "CONFIRMED"
    assert resp.json()["id"] == hold["id"]
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 8  # unchanged by confirm: stock was taken at reserve time
    assert snap["version"] == 1  # confirm doesn't touch the product row


async def test_double_confirm_is_idempotent(client, engine, make_product):
    product = await make_product(total_qty=STOCK)
    hold = await reserve(client, product["id"])
    first = await confirm(client, hold["id"])

    second = await confirm(client, hold["id"])

    assert second.status_code == 200
    assert second.json() == first.json()
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 8


async def test_confirm_released_409(client, engine, make_product):
    product = await make_product(total_qty=STOCK)
    hold = await reserve(client, product["id"])
    assert (await release(client, hold["id"])).status_code == 200

    assert_error(await confirm(client, hold["id"]), 409, "INVALID_STATE")
    await assert_invariant(engine, product["id"])


async def test_confirm_after_expiry_410(client, engine, make_product):
    # BUILDBOOK: hold with a past expires_at created directly in the DB; reaper hasn't run.
    product = await make_product(total_qty=STOCK)
    rid = await insert_hold(engine, product["id"], qty=2, expires_in_seconds=-5)

    assert_error(await confirm(client, rid), 410, "EXPIRED")

    got = (await client.get(f"/reservations/{rid}")).json()
    assert got["status"] == "HELD"  # confirm didn't change it; the reaper will
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 8  # still held until the reaper returns it


async def test_confirm_expired_status_410(client, engine, make_product):
    product = await make_product(total_qty=STOCK)
    rid = await insert_hold(engine, product["id"], qty=2, status="EXPIRED", expires_in_seconds=-5)

    assert_error(await confirm(client, rid), 410, "EXPIRED")
    await assert_invariant(engine, product["id"])


async def test_confirm_missing_404(client):
    assert_error(await confirm(client, uuid.uuid4()), 404, "NOT_FOUND")


# ---------- release ----------


async def test_release_held_returns_stock(client, engine, make_product):
    product = await make_product(total_qty=STOCK)
    hold = await reserve(client, product["id"], qty=3)

    resp = await release(client, hold["id"])

    assert resp.status_code == 200
    assert resp.json()["status"] == "RELEASED"
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == STOCK
    assert snap["version"] == 2  # reserve + release


async def test_double_release_returns_stock_once(client, engine, make_product):
    product = await make_product(total_qty=STOCK)
    hold = await reserve(client, product["id"], qty=3)
    first = await release(client, hold["id"])

    second = await release(client, hold["id"])

    assert second.status_code == 200
    assert second.json() == first.json()
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == STOCK  # not 13


async def test_release_after_confirm_409(client, engine, make_product):
    product = await make_product(total_qty=STOCK)
    hold = await reserve(client, product["id"], qty=2)
    assert (await confirm(client, hold["id"])).status_code == 200

    assert_error(await release(client, hold["id"]), 409, "INVALID_STATE")
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 8  # sold stock stays sold


async def test_release_expired_409(client, engine, make_product):
    product = await make_product(total_qty=STOCK)
    rid = await insert_hold(engine, product["id"], qty=2, status="EXPIRED", expires_in_seconds=-5)

    assert_error(await release(client, rid), 409, "INVALID_STATE")
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == STOCK  # the reaper already returned it; not returned again


async def test_release_held_past_expiry_returns_stock(client, engine, make_product):
    # Status is still HELD (reaper hasn't run), so release is allowed and does the reaper's job.
    product = await make_product(total_qty=STOCK)
    rid = await insert_hold(engine, product["id"], qty=2, expires_in_seconds=-5)

    resp = await release(client, rid)

    assert resp.status_code == 200
    assert resp.json()["status"] == "RELEASED"
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == STOCK


async def test_release_missing_404(client):
    assert_error(await release(client, uuid.uuid4()), 404, "NOT_FOUND")


async def test_released_stock_can_be_reserved_again(client, engine, make_product):
    product = await make_product(total_qty=2)
    hold = await reserve(client, product["id"], qty=2)
    await release(client, hold["id"])

    again = await reserve(client, product["id"], qty=2)

    assert again["status"] == "HELD"
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == 0


@pytest.mark.parametrize("path", ["/reservations/not-a-uuid/confirm", "/reservations/not-a-uuid"])
async def test_malformed_id_422(client, path):
    resp = await (client.post(path) if path.endswith("confirm") else client.delete(path))
    assert_error(resp, 422, "VALIDATION_ERROR")


# ---------- races ----------


async def test_concurrent_releases_return_stock_once(client, engine, make_product):
    product = await make_product(total_qty=STOCK)
    hold = await reserve(client, product["id"], qty=4)

    responses = await asyncio.gather(*(release(client, hold["id"]) for _ in range(10)))

    assert all(r.status_code == 200 for r in responses)
    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == STOCK  # +4 once, not +40 (and no CHECK/total violation)


async def test_confirm_vs_release_race_exactly_one_wins(client, engine, make_product):
    product = await make_product(total_qty=STOCK)
    holds = [await reserve(client, product["id"], qty=1) for _ in range(STOCK)]

    pairs = await asyncio.gather(
        *(asyncio.gather(confirm(client, h["id"]), release(client, h["id"])) for h in holds)
    )

    outcomes = Counter()
    for confirm_resp, release_resp in pairs:
        codes = (confirm_resp.status_code, release_resp.status_code)
        # Whoever locked the row first wins; the other sees the final state and gets 409.
        assert codes in {(200, 409), (409, 200)}, codes
        outcomes["confirmed" if codes == (200, 409) else "released"] += 1
    print(f"\noutcomes: {dict(outcomes)}")

    snap = await assert_invariant(engine, product["id"])
    assert snap["available"] == outcomes["released"]
    assert snap["reserved"] == outcomes["confirmed"]
