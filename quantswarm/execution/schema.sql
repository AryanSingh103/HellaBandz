-- QuantSwarm schema (TimescaleDB / Tiger Data). Idempotent: safe to run on every start.
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Durable, ordered input log. The API is the only writer; the execution worker
-- is the only consumer. command_id is the idempotency key.
CREATE TABLE IF NOT EXISTS commands (
    seq           BIGSERIAL PRIMARY KEY,
    command_id    TEXT NOT NULL UNIQUE,
    portfolio_id  TEXT NOT NULL,
    kind          TEXT NOT NULL,
    payload       JSONB NOT NULL,
    payload_hash  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    response      JSONB,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS commands_pending ON commands (portfolio_id, seq) WHERE status = 'pending';

-- Append-only, hash-chained ledger. Written only by the execution worker, in
-- the same transaction that marks the originating command done.
CREATE TABLE IF NOT EXISTS ledger_events (
    portfolio_id  TEXT NOT NULL,
    seq           BIGINT NOT NULL,
    command_seq   BIGINT NOT NULL REFERENCES commands(seq),
    type          TEXT NOT NULL,
    event         JSONB NOT NULL,
    hash          TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (portfolio_id, seq)
);

-- Projection of the ledger (rebuildable from ledger_events).
CREATE TABLE IF NOT EXISTS portfolio_state (
    portfolio_id  TEXT PRIMARY KEY,
    seq           BIGINT NOT NULL,
    head_hash     TEXT NOT NULL,
    digest        TEXT NOT NULL,
    state         JSONB NOT NULL,
    equity        BIGINT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Latest complete recovery checkpoint per portfolio. Replaced atomically with
-- events + projection; decisions are included to preserve retry idempotency.
CREATE TABLE IF NOT EXISTS ledger_checkpoints (
    portfolio_id TEXT PRIMARY KEY,
    version      INTEGER NOT NULL,
    seq          BIGINT NOT NULL CHECK (seq > 0),
    head_hash    TEXT NOT NULL,
    digest       TEXT NOT NULL,
    snapshot     JSONB NOT NULL,
    snapshot_hash TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    order_id       TEXT PRIMARY KEY,
    portfolio_id   TEXT NOT NULL,
    agent_id       TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,
    order_type     TEXT,
    requested_qty  BIGINT NOT NULL,
    approved_qty   BIGINT NOT NULL,
    filled_qty     BIGINT NOT NULL DEFAULT 0,
    limit_px       BIGINT,
    status         TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    rules          TEXT[] NOT NULL,
    signal_ts      TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orders_agent ON orders (portfolio_id, agent_id);

-- Time-series (hypertables).
CREATE TABLE IF NOT EXISTS fills (
    bar_ts        TIMESTAMPTZ NOT NULL,
    portfolio_id  TEXT NOT NULL,
    event_seq     BIGINT NOT NULL,
    order_id      TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    qty           BIGINT NOT NULL,
    px            BIGINT NOT NULL,
    fee           BIGINT NOT NULL,
    PRIMARY KEY (portfolio_id, event_seq, bar_ts)
);
SELECT create_hypertable('fills', 'bar_ts', if_not_exists => TRUE, migrate_data => TRUE);

CREATE TABLE IF NOT EXISTS equity_points (
    ts            TIMESTAMPTZ NOT NULL,
    portfolio_id  TEXT NOT NULL,
    equity        BIGINT NOT NULL,
    cash          BIGINT NOT NULL,
    market_value  BIGINT NOT NULL,
    PRIMARY KEY (portfolio_id, ts)
);
SELECT create_hypertable('equity_points', 'ts', if_not_exists => TRUE, migrate_data => TRUE);

CREATE TABLE IF NOT EXISTS market_bars (
    ts          TIMESTAMPTZ NOT NULL,
    dataset_id  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    open        BIGINT NOT NULL,
    high        BIGINT NOT NULL,
    low         BIGINT NOT NULL,
    close       BIGINT NOT NULL,
    volume      BIGINT NOT NULL,
    stress      BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (dataset_id, symbol, ts)
);
SELECT create_hypertable('market_bars', 'ts', if_not_exists => TRUE, migrate_data => TRUE);

CREATE TABLE IF NOT EXISTS telemetry (
    ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    service  TEXT NOT NULL,
    metric   TEXT NOT NULL,
    value    DOUBLE PRECISION NOT NULL,
    labels   JSONB
);
SELECT create_hypertable('telemetry', 'ts', if_not_exists => TRUE);

-- Research / tournament records.
CREATE TABLE IF NOT EXISTS tournaments (
    run_id         TEXT PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    manifest       JSONB NOT NULL,
    manifest_hash  TEXT NOT NULL,
    solana_sig     TEXT
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
