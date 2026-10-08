"""Load test: many users reserving 1 unit of the hot SKU (BUILDBOOK Phase 6).

    python loadtest/seed.py
    locust -f loadtest/locustfile.py --headless -u 200 -r 50 -t 30s --host http://localhost:8000 --csv loadtest/results

Outcomes:
  201  reserved              -> success
  409  OUT_OF_STOCK          -> success (expected once the 50 units are gone)
  503  BUSY (retries spent)  -> success in Locust stats, but counted separately
  anything else              -> failure

Each test (every Start in the web UI, or the single headless run) re-reads the HOT-1 id
that seed.py wrote, so re-seeding while the web UI stays open targets the new product.
When the test stops, per-status counts are printed and saved to
loadtest/results_status_counts.json (read by verify.py).
"""

import json
import os
import uuid
from collections import Counter
from pathlib import Path

from locust import HttpUser, between, events, task

HERE = Path(__file__).parent

# Per-test state: set on test_start, read by every user, reported on test_stop.
run = {"product_id": None, "status_counts": Counter()}


def _hot_product_id() -> int:
    if "HOT_PRODUCT_ID" in os.environ:
        return int(os.environ["HOT_PRODUCT_ID"])
    return json.loads((HERE / "hot_product.json").read_text())["product_id"]


@events.test_start.add_listener
def load_hot_product(environment, **kwargs):
    # Fires before any user spawns. Reading the id here (not at import) means a web UI
    # left open across re-seeds never hammers a product that no longer exists.
    run["product_id"] = _hot_product_id()
    run["status_counts"] = Counter()
    print(f"load test targeting HOT-1 product_id={run['product_id']}")


class Shopper(HttpUser):
    wait_time = between(0.01, 0.1)

    @task
    def reserve_hot_item(self):
        with self.client.post(
            "/reservations",
            json={"product_id": run["product_id"], "qty": 1},
            headers={"Idempotency-Key": str(uuid.uuid4())},  # every request is a new customer
            name="POST /reservations",
            catch_response=True,
        ) as resp:
            run["status_counts"][resp.status_code] += 1
            if resp.status_code in (201, 409, 503):
                resp.success()
            else:
                resp.failure(f"unexpected {resp.status_code}: {resp.text[:200]}")


@events.test_stop.add_listener
def report_status_counts(environment, **kwargs):
    # test_stop fires at the end of every test (headless run-time limit or Stop in the
    # web UI), so the file is written without having to quit Locust.
    counts = run["status_counts"]
    summary = {
        "product_id": run["product_id"],
        "status_counts": {str(code): n for code, n in sorted(counts.items())},
        "reserved_201": counts[201],
        "sold_out_409": counts[409],
        "busy_503": counts[503],
        "unexpected": sum(n for code, n in counts.items() if code not in (201, 409, 503)),
    }
    (HERE / "results_status_counts.json").write_text(json.dumps(summary, indent=2))
    print("\n=== reserve outcomes ===")
    print(json.dumps(summary, indent=2))
