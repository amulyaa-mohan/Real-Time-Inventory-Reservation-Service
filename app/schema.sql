-- Run on every startup by app/db.py; every statement is idempotent (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS products (
    id          BIGSERIAL PRIMARY KEY,
    sku         TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    total_qty   INTEGER NOT NULL CHECK (total_qty >= 0),
    available   INTEGER NOT NULL CHECK (available >= 0),   -- DB-level last line of defence
    version     INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reservations (
    id               UUID PRIMARY KEY,
    product_id       BIGINT NOT NULL REFERENCES products(id),
    qty              INTEGER NOT NULL CHECK (qty > 0),
    status           TEXT NOT NULL CHECK (status IN ('HELD','CONFIRMED','RELEASED','EXPIRED')),
    idempotency_key  TEXT NOT NULL UNIQUE,
    request_hash     TEXT NOT NULL,
    expires_at       TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Reaper scans only live holds by expiry
CREATE INDEX IF NOT EXISTS idx_reservations_held_expiry
    ON reservations (expires_at) WHERE status = 'HELD';
