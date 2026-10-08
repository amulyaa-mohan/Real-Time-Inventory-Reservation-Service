# Inventory Reservation Service
- Run: `docker compose up -d --build`; API on :8000
- Test: `pytest -q` (needs postgres + redis from compose running)
- Load test: see BUILDBOOK.md Phase 6
- Critical SQL lives in app/services/*.py as sqlalchemy text(); keep it raw and explicit, no ORM
- IMPORTANT: never remove the `available >= :qty` condition, the CHECK constraints, or the idempotency UNIQUE constraint
- Run the relevant tests after every change and show the output
- Do not add auth, new frameworks, or features not in BUILDBOOK.md without asking
