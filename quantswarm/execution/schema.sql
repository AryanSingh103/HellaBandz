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
-- supports the foreign key (and "which events did this command produce?")
CREATE INDEX IF NOT EXISTS ledger_events_command ON ledger_events (command_seq);

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
    order_id       TEXT NOT NULL,
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
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (portfolio_id, order_id)
);
CREATE INDEX IF NOT EXISTS orders_agent ON orders (portfolio_id, agent_id);

-- Additive projection migration: ledger/checkpoint bytes are never rewritten.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'orders'::regclass
               AND contype = 'p' AND array_length(conkey, 1) = 1) THEN
        ALTER TABLE orders DROP CONSTRAINT orders_pkey;
        ALTER TABLE orders ADD PRIMARY KEY (portfolio_id, order_id);
    END IF;
END $$;

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
ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS portfolio_id TEXT;

CREATE TABLE IF NOT EXISTS portfolio_context (
    portfolio_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES tournaments(run_id),
    dataset_id TEXT,
    session_job_id TEXT,
    provenance TEXT NOT NULL DEFAULT 'browser',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS managed_workers (
    portfolio_id TEXT PRIMARY KEY,
    desired TEXT NOT NULL DEFAULT 'running' CHECK (desired IN ('running', 'down')),
    status TEXT NOT NULL DEFAULT 'starting',
    generation BIGINT NOT NULL DEFAULT 0,
    error TEXT,
    started_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE managed_workers ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('research', 'session')),
    dataset_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    portfolio_id TEXT,
    config JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    total INTEGER,
    stop_requested BOOLEAN NOT NULL DEFAULT FALSE,
    result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_portfolio ON jobs (portfolio_id)
    WHERE kind = 'session' AND status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (created_at) WHERE status = 'queued';

-- Local strategy-license exchange. Credits are separate from trading capital.
CREATE TABLE IF NOT EXISTS review_analyses (
    digest TEXT PRIMARY KEY,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS strategy_accounts (
    account_id TEXT PRIMARY KEY,
    balance BIGINT NOT NULL CHECK (balance >= 0),
    credit_grant BIGINT NOT NULL CHECK (credit_grant >= 0)
);
CREATE TABLE IF NOT EXISTS strategy_assets (
    asset_id TEXT PRIMARY KEY,
    descriptor JSONB NOT NULL,
    supply INTEGER NOT NULL CHECK (supply > 0)
);
CREATE TABLE IF NOT EXISTS strategy_holdings (
    account_id TEXT REFERENCES strategy_accounts(account_id),
    asset_id TEXT REFERENCES strategy_assets(asset_id),
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    PRIMARY KEY (account_id, asset_id)
);
CREATE TABLE IF NOT EXISTS strategy_listings (
    listing_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    config JSONB NOT NULL,
    asset_id TEXT NOT NULL REFERENCES strategy_assets(asset_id),
    seller_id TEXT NOT NULL REFERENCES strategy_accounts(account_id),
    price BIGINT NOT NULL CHECK (price > 0),
    remaining INTEGER NOT NULL CHECK (remaining >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS strategy_trades (
    request_id TEXT PRIMARY KEY,
    config JSONB NOT NULL,
    listing_id TEXT NOT NULL REFERENCES strategy_listings(listing_id),
    asset_id TEXT NOT NULL REFERENCES strategy_assets(asset_id),
    buyer_id TEXT NOT NULL REFERENCES strategy_accounts(account_id),
    seller_id TEXT NOT NULL REFERENCES strategy_accounts(account_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price BIGINT NOT NULL CHECK (price > 0),
    total BIGINT NOT NULL CHECK (total = quantity * price),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Atomic before/after images let a sealed snapshot be checked while trading continues.
-- Sequence gaps and out-of-order transaction commits are handled by the verifier's
-- explicit sealed ID ranges, rather than assuming MAX(id) means a committed prefix.
CREATE TABLE IF NOT EXISTS audit_changes (
    id BIGSERIAL PRIMARY KEY,
    table_name TEXT NOT NULL,
    operation TEXT NOT NULL,
    before_row JSONB,
    after_row JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE OR REPLACE FUNCTION qs_record_change() RETURNS trigger LANGUAGE plpgsql SET timezone='UTC' AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND to_jsonb(OLD) = to_jsonb(NEW) THEN RETURN NEW; END IF;
    INSERT INTO audit_changes(table_name, operation, before_row, after_row)
    VALUES (TG_ARGV[0], TG_OP,
            CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END,
            CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END);
    RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION qs_deny_audit_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit history cannot be rewritten or truncated';
END $$;
DROP TRIGGER IF EXISTS qs_audit_immutable ON audit_changes;
CREATE TRIGGER qs_audit_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_changes
FOR EACH STATEMENT EXECUTE FUNCTION qs_deny_audit_rewrite();
DO $$ DECLARE t TEXT; BEGIN
    FOREACH t IN ARRAY ARRAY['commands','ledger_events','portfolio_state','orders','fills',
        'equity_points','agent_messages','tournaments','portfolio_context','jobs',
        'strategy_accounts','strategy_assets','strategy_holdings','strategy_listings',
        'strategy_trades','review_analyses'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = t::regclass AND tgname = 'qs_audit_change') THEN
            EXECUTE format('CREATE TRIGGER qs_audit_change AFTER INSERT OR UPDATE OR DELETE ON %I
                            FOR EACH ROW EXECUTE FUNCTION qs_record_change(%L)', t, t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = t::regclass AND tgname = 'qs_audit_no_truncate') THEN
            EXECUTE format('CREATE TRIGGER qs_audit_no_truncate BEFORE TRUNCATE ON %I
                            FOR EACH STATEMENT EXECUTE FUNCTION qs_deny_audit_rewrite()', t);
        END IF;
    END LOOP;
END $$;
CREATE TABLE IF NOT EXISTS audit_anchors (
    anchor_id TEXT PRIMARY KEY,
    manifest JSONB NOT NULL,
    manifest_hash TEXT NOT NULL,
    signature TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS audit_requests (
    request_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    error TEXT
);
CREATE TABLE IF NOT EXISTS audit_service_state (
    id INTEGER PRIMARY KEY CHECK (id=1),
    heartbeat TIMESTAMPTZ NOT NULL,
    error TEXT
);
