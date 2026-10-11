# HellaBandz

**Autonomous AI strategy agents on trading infrastructure they cannot break.**

## Measured results

These are the completed baseline measurements before operator authentication and the full-trace
audit journal. The current security build needs a fresh complete benchmark before these can be
used as its performance claims.

- **Pre-trade risk check:** p50 1.8 µs, p99 3.1 µs (in-process).
- **Durable order path** (HTTP → Postgres command log → worker → atomic ledger commit → response), 1 vCPU worker + 1 vCPU API + 2 vCPU DB: **482 orders/s** at 50 concurrent clients (p99 168 ms); 9.0 ms p50 for a single client.
- **Crash recovery:** 105,000-event ledger restored from checkpoint + 5,000-event verified suffix in 0.44 s; container restart to ready 2.0 s.
- **Replay:** 131,040 real Binance 1-minute bars backtested at 37,916 bars/s; event-log replay 96,308 events/s.

These are Windows/i9 measurements on the updated core. Container limits: worker and API each 1 vCPU/512 MiB, DB 2 vCPU/2 GiB, load client 1 vCPU/512 MiB. See PERFORMANCE.md for repetitions, percentiles, scaling tables and source hash.

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

The current judge demo also includes operator access control and a working full-trace
Solana Devnet seal: 9,603 records across 16 record sets, including 106 agent messages
and two virtual-license sales. The Audit screen verifies the finalized transaction
against the retained records. This proves sealed record integrity, not trading profitability.

The no-key path uses a synthetic dataset and recorded agent proposals. Historical Binance bars support
the replay benchmark. Combined verification also exercised live OpenAI research/supervisor/crisis/Q&A,
ElevenLabs audio, Alpaca ingestion and a real-data tournament/session, and verified an existing Solana
Devnet commitment. These integrations stay outside the deterministic execution path. There are no real
orders, funds or brokerage connections. See [VERIFICATION](VERIFICATION.md) for exact run evidence.

## The 10 questions

| | |
|---|---|
| **User and problem** | A desk letting autonomous agents propose trades needs a hard boundary that keeps a buggy or overconfident agent from overspending, double-filling, shorting, or corrupting the books. |
| **Core claim** | No sequence of duplicated, concurrent, malformed, delayed, or crash-interrupted commands can create cash or shares from nothing or apply a fill twice, and the ledger can always be rebuilt bit-for-bit from its event log. |
| **Boundary** | Bar-based simulated fills (no order book or queue position), long-only cash account, 5 symbols, single-operator authentication. See [LIMITATIONS](LIMITATIONS.md). |
| **Correctness** | 12 invariant categories (idempotency, cash/position safety, risk enforcement, fill validity, determinism, accounting identity, recovery, causality, audit integrity, ordering and operator access): [INVARIANTS](INVARIANTS.md). |
| **Evidence** | The proof suite covers unit, property-based (random command programs checked after every step), real-Postgres concurrency, SIGKILL fault injection, tamper detection. `make prove` prints the invariant table. |
| **Interfaces** | HTTP API ⇄ Postgres command log ⇄ execution worker; agents and UI are API clients. [ARCHITECTURE](ARCHITECTURE.md). |
| **Measurement** | Risk-check latency, durable order throughput at 1/10/50 clients, replay speed on 131k real Binance bars, recovery time vs ledger size, all under fixed container limits: [PERFORMANCE](PERFORMANCE.md). |
| **Failure behavior** | Invalid ⇒ 422 or recorded rejection with rule code. Duplicate ⇒ original decision. Same id, new payload ⇒ 409. Stale or future signal ⇒ R7. Out-of-order bar ⇒ rejected, no effect. Worker down ⇒ commands queue (HTTP 202) and drain in order on restart. Corrupted checkpoint, suffix or projection ⇒ worker refuses to start; full-history verification audits the saved prefix. |
| **Reproduction** | Commands below, no API keys needed (committed dataset + recorded agent proposals). |
| **Next limit** | Incremental agent exposure and recovery checkpoints are implemented. Next: shard portfolios, profile position/fill/projection scans, and archive growing event and retry histories. |

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
long-only cash accounting and shared demo identities remain boundaries. Operator authentication and full-trace Devnet commitments are implemented; see [SECURITY](SECURITY.md).

## Reproduce and demonstrate

From the repository root, follow the README requirements, then run:

```bash
make setup
make up
make tournament
make session
make prove
uv run python scripts/ingest_binance.py  # public, checksum-verified; no key needed
make bench
```

The Binance download is optional; without it the benchmark uses the committed synthetic fixture and
explicitly reports which larger recovery sizes are unavailable. `make crash-worker` and
`make restart-worker` demonstrate durable queueing and recovery. The automated suite also crashes before
and after transaction commit, repeatedly terminates the worker, and corrupts stored evidence.

Source: https://github.com/AryanSingh103/HellaBandz

See `docs/PERFORMANCE.md` for measured percentiles, workloads, hardware, container limits and exact procedures;
`docs/INVARIANTS.md` for the correctness claims; and `docs/LIMITATIONS.md` for unsupported behavior.
