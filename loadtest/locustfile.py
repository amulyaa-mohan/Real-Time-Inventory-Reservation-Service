"""Load test: many users reserving 1 unit of the hot SKU (BUILDBOOK Phase 6).

    python loadtest/seed.py
    locust -f loadtest/locustfile.py --headless -u 200 -r 50 -t 30s --host http://localhost:8000 --csv loadtest/results

Outcomes:
  201  reserved              -> success
  409  OUT_OF_STOCK          -> success (expected once the 50 units are gone)
  503  BUSY (retries spent)  -> success in Locust stats, but counted separately
  anything else              -> failure
Per-status counts are printed at the end and saved to loadtest/results_status_counts.json.
"""

import json
import os
import uuid
from collections import Counter
from pathlib import Path

from locust import HttpUser, between, events, task

HERE = Path(__file__).parent


def _hot_product_id() -> int:
    if "HOT_PRODUCT_ID" in os.environ:
        return int(os.environ["HOT_PRODUCT_ID"])
    return json.loads((HERE / "hot_product.json").read_text())["product_id"]


PRODUCT_ID = _hot_product_id()
status_counts: Counter = Counter()


class Shopper(HttpUser):
    wait_time = between(0.01, 0.1)

    @task
    def reserve_hot_item(self):
        with self.client.post(
            "/reservations",
            json={"product_id": PRODUCT_ID, "qty": 1},
            headers={"Idempotency-Key": str(uuid.uuid4())},  # every request is a new customer
            name="POST /reservations",
            catch_response=True,
        ) as resp:
            status_counts[resp.status_code] += 1
            if resp.status_code in (201, 409, 503):
                resp.success()
            else:
                resp.failure(f"unexpected {resp.status_code}: {resp.text[:200]}")


@events.quitting.add_listener
def report_status_counts(environment, **kwargs):
    counts = {str(code): n for code, n in sorted(status_counts.items())}
    summary = {
        "product_id": PRODUCT_ID,
        "status_counts": counts,
        "reserved_201": status_counts[201],
        "sold_out_409": status_counts[409],
        "busy_503": status_counts[503],
        "unexpected": sum(n for code, n in status_counts.items() if code not in (201, 409, 503)),
    }
    (HERE / "results_status_counts.json").write_text(json.dumps(summary, indent=2))
    print("\n=== reserve outcomes ===")
    print(json.dumps(summary, indent=2))
