# QuantSwarm

**Autonomous AI strategy agents on trading infrastructure they cannot break.**

## Inspiration

An autonomous trading agent can produce a convincing explanation and still submit a dangerous order.
We focused on the boundary between an agent's proposal and the portfolio's money: every proposal must
pass deterministic risk rules, and every committed effect must survive retries and crashes.

## What it does

Three research agents propose whitelisted strategies. A deterministic tournament fits parameters on
training data, ranks candidates on validation data, freezes allocations, then evaluates held-out data.
The paper session replays that held-out period through an HTTP API and an isolated execution worker.
A scripted market crash provokes oversized orders, risk reductions, rejections and de-risking.
The terminal exposes decisions, fills, portfolio state and replay verification.

The default dataset is synthetic and the default agent proposals are recorded fixtures. Historical
Binance bars support the replay benchmark. Optional live LLM, voice, Alpaca and Solana Devnet paths are
separate integrations; their presence in the code is not a claim that every keyed service was exercised
in this core-performance verification. There are no real orders, funds or brokerage connections.

## How we built it

The core is a pure Python state machine shared by the worker, backtester, replay and tests. Money uses
integer micro-dollars. The API only inserts commands into Postgres; one advisory-locked worker per
portfolio performs risk checks and commits ledger events, projections and responses atomically.
Events form a SHA-256 chain. Per-agent exposure and buy reservations are maintained by events, with
independent recomputation in invariant checks.

Every 10,000 events, at a completed batch boundary, the worker also commits a complete checkpoint.
Startup verifies its hash, digest and ledger anchor, replays the suffix and checks the final projection
digest. Retry decisions are included so a restart cannot turn a duplicate into a new trade.
Full-history verification remains available independently of checkpoints.

FastAPI and Pydantic provide interfaces and validation; psycopg connects to PostgreSQL/TimescaleDB.
LangGraph orchestrates research; NumPy and PyArrow support strategies and frozen datasets; Next.js/React
provide the terminal. pytest and Hypothesis test the core, real-DB concurrency and forced termination.
Docker fixes CPU/memory limits for durable throughput and cold-start measurements. The README's
"What we built vs. what we used" table and `docs/DATA_SOURCES.md` provide the full dependency/data disclosure.

## Challenges and engineering decisions

We chose one writer and one durable store so consuming a command and changing money share a transaction.
Batching amortizes commits while preserving atomic rollback. We keep LLM latency out of engine measurements
and use recorded inputs for reproduction. Incremental risk totals remove repeated order scans; checkpoints
reduce replay work while preserving an independent full-history audit.

A checkpoint trusts its historical prefix. Startup checks the snapshot and suffix; `/v1/verify` detects
changes to earlier event payloads. Hashes detect corruption, not an attacker who can rewrite all DB hashes.
We preserve this distinction in the documentation and test it explicitly.

## What we learned and what comes next

Reliable trading infrastructure needs clear failure semantics before a sophisticated strategy. A negative
held-out result is still a valid result; the default tournament reports it. A fast risk function alone
does not establish a fast durable system, so we measure both under disclosed workloads.

The next limits are the single writer per portfolio, position marking and fill scans, state hashing per
batch, and growing event/retry histories. We would first shard independent portfolios, profile the remaining
scans, then design archival and retry-retention policies. Bar-based fills, a five-symbol default universe,
long-only cash accounting and an unauthenticated local API remain deliberate boundaries.

## Reproduce and demonstrate

From the repository root, follow the README requirements, then run:

```bash
make setup
make up
make tournament
make session
make web
make prove
uv run python scripts/ingest_binance.py  # public, checksum-verified; no key needed
make bench
```

The Binance download is optional; without it the benchmark uses the committed synthetic fixture and
explicitly reports which larger recovery sizes are unavailable. `make crash-worker` and
`make restart-worker` demonstrate durable queueing and recovery. The automated suite also crashes before
and after transaction commit, repeatedly terminates the worker, and corrupts stored evidence.

Source: https://github.com/AryanSingh103/QuantSwarm

See `docs/PERFORMANCE.md` for measured percentiles, workloads, hardware, container limits and exact procedures;
`docs/INVARIANTS.md` for the correctness claims; and `docs/LIMITATIONS.md` for unsupported behavior.
