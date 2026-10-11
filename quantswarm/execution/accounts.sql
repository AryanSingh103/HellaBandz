-- Application DB migration, applied automatically alongside schema.sql.
-- Supabase identity is external; avoid a cross-database auth.users FK.
CREATE TABLE IF NOT EXISTS account_resources (
    kind TEXT NOT NULL CHECK (kind IN ('portfolio', 'run', 'job', 'request')),
    resource_id TEXT NOT NULL,
    owner_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, resource_id)
);
CREATE INDEX IF NOT EXISTS account_resources_owner ON account_resources(owner_id, kind);
CREATE TABLE IF NOT EXISTS kalshi_connections (
    owner_id UUID NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('demo', 'production')),
    encrypted_credentials TEXT NOT NULL,
    key_hint TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, environment)
);
ALTER TABLE account_resources ENABLE ROW LEVEL SECURITY;
ALTER TABLE kalshi_connections ENABLE ROW LEVEL SECURITY;
-- Neither table has public policies. Only the private API/manager database
-- role (table owner) may access these, after HTTP identity/scope enforcement.
REVOKE ALL ON account_resources, kalshi_connections FROM PUBLIC;
CREATE TABLE IF NOT EXISTS kalshi_demo_order_attempts (
    owner_id UUID NOT NULL,
    client_order_id UUID NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitting',
    response JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, client_order_id)
);
ALTER TABLE kalshi_demo_order_attempts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON kalshi_demo_order_attempts FROM PUBLIC;
