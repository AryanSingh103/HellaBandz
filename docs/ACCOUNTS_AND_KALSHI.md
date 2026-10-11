# Accounts and Kalshi setup

HellaBandz now requires an individual Supabase account. Signup, email confirmation,
signin, persistent session refresh, signout, and password recovery are supported.
The API independently verifies every account bearer token with Supabase's Auth
service. Missing configuration locks account APIs; `/healthz` remains public.

## Configure accounts

1. Create a Supabase project and enable email/password authentication. Set its
   **Site URL** to the web app origin and add that origin to allowed redirect URLs
   (locally, `http://localhost:3000`). Keep email confirmation enabled for deployment.
2. Copy `.env.example` settings into your existing root `.env`. Set
   `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` using the project's URL and
   publishable key (the legacy anon key is also supported). Set
   `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` to the same
   project values. **Do not use a Supabase service-role or secret key in the browser.**
3. Generate two distinct server secrets:

   ```sh
   uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
   uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Set the first as `QS_INTERNAL_SIGNING_KEY` and the second as
   `QS_CREDENTIAL_ENCRYPTION_KEY`. Preserve the encryption key across redeployments
   and back it up securely. Changing it makes stored Kalshi keys unreadable until
   connections are re-created. Never put either secret in a `NEXT_PUBLIC_` variable.
4. Set `QS_ALLOWED_ORIGINS` to the web app origin (comma-separated if needed).
   Deployed API and web endpoints must use HTTPS.
5. Keep `DATABASE_URL` / `QS_DATABASE_URL` pointing at the existing TimescaleDB /
   TigerData database. Supabase handles identity; its ordinary database is **not**
   a drop-in replacement for this application's TimescaleDB schema.
6. Apply the updated deployment when ready: `docker compose up -d --build`.
   Frontend `NEXT_PUBLIC_` values are baked into the bundle, so changes require a
   web rebuild. For host-based Next development, copy `apps/web/.env.local.example`
   to `apps/web/.env.local` and fill its values as well as the root server `.env`.

The additive `hellabandz/execution/accounts.sql` migration runs automatically on
API/worker startup. It adds resource ownership, encrypted connections, and demo
order attempts. All have row-level security enabled and no public access policies.
These are private application database tables accessed through the API, not
Supabase browser queries; the API/manager connect as the private table owner.
Do not expose the application database credentials or use its table-owner role
for browser/PostgREST access. Existing ledger bytes and research files are unchanged.

Each user receives a separate empty research portfolio. No real exchange funds
are created. Research session capital stays virtual. Existing portfolios, jobs,
and tournaments are deliberately unowned and invisible to signed-in accounts;
they are not adopted by guessing an old ID. Shared registered market datasets
remain available, while API-injected bars are namespaced by account. Account
portfolio/job/run IDs and downloads, history, messages, runtime actions, and
WebSocket streams are authorized server-side. Switching users unmounts the
workspace and clears its in-memory state and sockets.

Managed jobs receive expiring (24-hour), HMAC-signed credentials scoped to their owner,
run, portfolio, and required endpoints. The manager needs the same
`QS_INTERNAL_SIGNING_KEY` as the API. No global admin bearer bypass is exposed.

## Connect Kalshi

Open **Account & Kalshi**, select **Demo**, and provide the API key ID and matching
Ed25519 or RSA private key PEM from your Kalshi demo account. The backend makes
a real signed balance request to verify it before storing the credentials with
Fernet authenticated encryption. It returns only key suffix and verification
time. Credentials are per user and per environment; changing environment never
reuses another environment's key. Failed verification preserves an existing
connection. Disconnect removes stored credentials; revoke the API key in Kalshi
when you also want to invalidate it at the exchange.

The UI reads actual exchange balances, recent orders, positions, and open markets.
Demo balances are test funds. Production connections are read-only and show
actual account balances. There is **no production order route**.

To test an exchange order, explicitly set `QS_KALSHI_DEMO_ORDERS_ENABLED=true`
and restart the API. The account UI then offers manual whole-contract YES buys
on the **demo exchange only**, using the V2 events order endpoint, bid side,
limit price, immediate-or-cancel, and self-trade prevention. The per-request
premium cap is $5 **before fees**. The exchange may fill all, some, or none of
the order. Production cannot be selected in this route. Requests require a
unique client order UUID and explicit demo acknowledgment. A durable attempt
record prevents resubmission after uncertain network outcomes; inspect the
exchange's orders before creating a new request. Persisted attempts are visible
after reload; an unresolved attempt blocks even a new UUID until you explicitly
acknowledge reviewing exchange orders. No automatic POST retries occur.

The existing equity/crypto research swarm still runs historical simulations.
Autonomous Kalshi execution requires a prediction-market strategy adapter,
market resolution and settlement accounting, portfolio-wide exposure/budget
limits, exchange order/fill reconciliation, and explicit production activation.
Those are separate from account connection and manual demo order testing. This
change does not promise a return or automatically deploy real funds.

## Local offline development

For an intentionally offline, local-only research workspace, set both
`QS_AUTH_DISABLED=true` and `NEXT_PUBLIC_QS_AUTH_DISABLED=true`, then rebuild
the frontend. This restores the previous shared local behavior and displays
a development banner. The account/Kalshi screens require a real identity even
in this mode. Never deploy the opt-out publicly. Existing infrastructure tests
explicitly use this mode; account-security tests exercise secure mode separately.

Official references: [Supabase Auth](https://supabase.com/docs/guides/auth),
[Kalshi API keys](https://docs.kalshi.com/getting_started/api_keys),
[Kalshi environments](https://docs.kalshi.com/getting_started/api_environments),
[Kalshi V2 orders](https://docs.kalshi.com/api-reference/orders/create-order-v2).

## Local judge demo compatibility

The default `QS_AUTH_MODE=accounts` uses the private account flow above. Set
`QS_AUTH_MODE=operator` explicitly to retain a shared local demo with the operator
key and HttpOnly session sign-in. This mode bypasses account ownership only behind
the operator access gate; it is unsuitable for a multi-user deployment. The existing
local `.env` keeps operator mode. Global Solana trace proof controls remain operator-only.
