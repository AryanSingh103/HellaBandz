# Browser runtime and deployment

`make up` starts TimescaleDB, the external `main` worker, API, dedicated local manager and web app.
`make backend` rebuilds only db/worker/api/manager. Both preserve the existing `pgdata` volume. Never
use `make reset` or `docker compose down -v` when retaining history. URLs: http://localhost:3000 and
http://localhost:8000/docs. Sign in through the terminal with QS_API_TOKEN from ignored .env. API routes and streams require operator access; see [SECURITY](SECURITY.md).

The API and manager share `data/snapshots` read-only and `data/manifests` read-write. The committed
synthetic fixture is included in the root Docker image. `/v1/datasets` returns registered IDs, source,
content hash, quality, periods, chronological splits, units and execution availability. Jobs accept only
these IDs, never filesystem paths or shell commands. Keys are passed to backend containers only.

`POST /v1/jobs` queues research or a session. Retry with the same `request_id` and identical body to
rediscover the same job; conflicting content returns 409. Research uses the actual tournament pipeline
and persists its finalized manifest plus a separate `.analytics.json` baseline artifact. Research is
serialized by the manager and at most four job children run concurrently. The API remains responsive.
`GET /v1/jobs` persists progress across refresh; statuses are queued/running/completed/failed/stopped/
interrupted. Errors shown to the browser omit provider bodies and paths; detailed local logs are under
`data/manifests/job-logs`. Missing provider keys select explicitly labeled fixture/fallback decisions.

Sessions require a frozen manifest matching the registered dataset hash and splits, fixed 10,000 quote
units of capital, and a fresh isolated portfolio ID. The portfolio is reserved and associated with its
run in the same transaction as the job. Each session waits for required command responses before using
the projection. Bounded steps, speed and an optional price-crash overlay are stored in the job config.
Stopping prevents future production and preserves queued commands, working orders, positions and cash.
Stopping while execution is down does not require restarting the worker. A manager restart marks
running jobs interrupted and never silently relaunches an economic session. Repeat a period with a new
portfolio rather than clearing the ledger.

`/v1/runtime?portfolio_id=...` reports the selected heartbeat, pending commands, ownership, readiness,
recovery timing, manager heartbeat and intentional-down state. `POST /v1/runtime` accepts only crash
and restart for manager-owned children; externally managed workers return 409. The manager never
controls Docker, arbitrary PIDs or host processes. Child workers retain their own advisory writer lock.
Crash is distinct from a scheduled market overlay. Intentional-down survives manager restart.

`schema.sql` applies an idempotent migration from global order identity to `(portfolio_id, order_id)`;
fills and closure updates are scoped to both keys. Existing event payloads, checkpoints, projections
and digests remain unchanged. Stop **all old-version workers**, including manually started external
workers, before applying this migration: their old global projection UPDATE statements are unsafe once
different portfolios reuse an order ID. Recreate external workers with the rebuilt worker image and the
same `--portfolio` and database configuration, keeping them externally owned. Do not reserve or adopt
them through `/v1/portfolios`.

Historical associations are imported only from an existing session artifact whose final digest agrees
with the stored portfolio and whose tournament exists. Other historical portfolios remain unassociated.
Finalized tournaments verify canonical hashes and run identity; identical saves are idempotent and
changed content requires a new run ID. Derived analytics and job metadata never alter frozen manifests.

History uses a frozen ledger-sequence boundary and descending immutable cursors; orders reconstruct
filled quantities and closure at that boundary. `/v1/orders/{id}` connects scoped commands/events.
`/v1/replay/{seq}` reconstructs before/after state without writes; `/v1/verify` independently audits the
full ledger. An empty unopened portfolio is neutral `not_opened`. Analytics derive agent realized P&L
plus marked holdings minus remaining basis and fees; exposure is holdings times last applied close.
Throughput counts processed portfolio commands per ten-second wall-clock bucket. Series are sampled
from real ledger points with their stride disclosed.

The baseline uses equal target weights over frozen dataset symbols and the same capital, period and
engine costs. Orders are decided after the first evaluation bar, fill on later bars, and expire under the
same rules. It targets 90% gross exposure with a cash buffer; unfilled quantities remain cash, without
rebalancing or final liquidation. Research benchmark series use the full held-out period and do not
include a session's optional crash overlay or a shorter session's steps. Legacy missing artifacts are
reported unavailable; they can be derived separately without rewriting the manifest.

Validation: `docker compose run --rm prove` executes the complete Linux suite against disposable test
databases. Browser contracts, composite-key migration, crypto accounting, owned-worker crash/restart,
session stop and manager reconciliation have focused regressions in `tests/integration` and `tests/unit`.
The graceful-manager restart regression is POSIX-only and is exercised by the Linux proof.
