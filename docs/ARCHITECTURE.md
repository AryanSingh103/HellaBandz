# Architecture

QuantSwarm separates **who proposes trades** (LLM research agents, untrusted) from **what is allowed to
happen to money** (a deterministic, single-writer execution worker). The interesting part is the
boundary, not the model.

```
                    ┌──────────────────────── untrusted / nondeterministic ───────────────────────┐
                    │                                                                             │
  frozen dataset ──►│  Agent service (LangGraph + OpenAI, or recorded fixtures)                   │
  (Parquet + hash)  │   summarize(train) → momentum | mean_reversion | volatility (parallel LLM)  │
                    │   → fit: deterministic grid search on TRAIN                                 │
                    │   → rank: deterministic backtests on VALIDATION + 3 stability folds + gates │
                    │   → supervisor (LLM, advisory) → deterministic allocator → test ONCE         │
                    │   → manifest (sha256) ──────────────────────────► Solana Devnet memo (opt.) │
                    │                                                                             │
                    │  Session runner: replays held-out bars + crash overlay, turns frozen        │
                    │  strategy signals (and LLM crisis responses) into ordinary HTTP orders      │
                    └───────────────────────────────┬─────────────────────────────────────────────┘
                                                    │ HTTP (OrderRequest, Bar, admin commands)
                    ┌───────────────────────────────▼─────────────────────────────────────────────┐
                    │  API gateway (FastAPI)                                                      │
                    │   • validates schemas (422 on malformed input)                              │
                    │   • INSERT INTO commands … ON CONFLICT (command_id) DO NOTHING  ← idempotency│
                    │   • 409 if an id is reused with a different payload                         │
                    │   • waits for the worker's NOTIFY (or returns 202 "queued" if it is down)   │
                    │   • read models, /v1/verify (independent replay), WebSocket event stream    │
                    └───────────────────────────────┬─────────────────────────────────────────────┘
                                                    │ Postgres: commands table = durable ordered input log
                    ┌───────────────────────────────▼─────────────────────────────────────────────┐
                    │  Execution worker (one per portfolio, pg_advisory_lock = single writer)     │
                    │   claim batch (FOR UPDATE SKIP LOCKED, seq order)                           │
                    │   → ledger.process(): risk rules R0–R9 + bar-based fill model (pure)        │
                    │   → check_invariants()                                                      │
                    │   → ONE transaction: ledger_events (hash-chained) + command responses       │
                    │     + projections (orders, fills, equity) + state digest                    │
                    │   → NOTIFY cmd_done                                                         │
                    │   startup: replay event log → verify chain + digest → refuse on mismatch    │
                    └───────────────────────────────┬─────────────────────────────────────────────┘
                                                    │
                    ┌───────────────────────────────▼─────────────────────────────────────────────┐
                    │  TimescaleDB (Tiger Data): commands, ledger_events, portfolio_state, orders │
                    │  hypertables: fills, equity_points, market_bars, telemetry                  │
                    └───────────────────────────────┬─────────────────────────────────────────────┘
                                                    │ REST + WebSocket
                                       Next.js trading terminal (apps/web)
```

## Components and why each boundary exists

| Component | Code | Responsibility | Why it is separate |
|---|---|---|---|
| Ledger core | `quantswarm/ledger/` | Pure state machine: `process(state, command) → events`, `apply_event`, invariants, digests | Shared by worker, backtester, recovery and tests, so "backtest == live" by construction |
| Execution worker | `quantswarm/execution/worker.py` | Only writer of financial state. Batches commands, commits events + projections atomically | Single writer per portfolio removes races without distributed locks; crash domain is isolated from the API |
| API gateway | `quantswarm/api/app.py` | Schema validation, idempotent command intake, read models, verification | Lets the worker die without losing requests (commands queue durably) |
| Backtester | `quantswarm/backtester/` | `DataProvider`, `Strategy`, `ExecutionModel`, `Portfolio`, `ResultStore` over the same engine | No second implementation of fills or accounting |
| Strategies | `quantswarm/strategies/` | Whitelisted causal families; agents choose parameters only | LLMs never ship executable code |
| Agents | `quantswarm/agents/` | LangGraph tournament, live session client, grounded Q&A | Nondeterministic, so kept strictly upstream of the command log |
| Audit | `quantswarm/audit/solana.py` | Manifest hash → SPL Memo on Devnet; verifier | Optional, outside the core path |
| Terminal | `apps/web/` | Dashboards, adversarial console, event inspector, voice Q&A | Observation and manual commands only, through the public API |

## Data flow of one order

1. Agent (or the UI) posts `OrderRequest{client_order_id, agent_id, symbol, side, qty, type, limit, signal_bar_ts}`.
2. API inserts `commands(command_id="order:<client_order_id>")`. A retry hits `ON CONFLICT` and returns the
   original decision; a different payload under the same id returns 409.
3. Worker claims the command, runs `evaluate_order` (R0–R9). Outcome is APPROVE, REDUCE (quantity cut to the
   tightest limit) or REJECT, recorded as an event either way. Buys reserve cash at last close + 2% + fees.
4. On the next bar for that symbol, the fill model fills at the open ± slippage (or at the limit), capped at
   1% of bar volume and by the order's reserved cash; leftovers expire after 5 bars and release their reservation.
5. All events, the command response, projections and the new state digest commit in one transaction.

## Risk totals and recovery checkpoints

`apply_event` maintains per-agent position cost basis and reserved buy cash on acceptance, partial
fills, sells and order closure. R2 uses two dictionary lookups; R3/R4 use the existing global cash
reservation total. `check_invariants` independently recomputes the sums each batch and property tests
compare them after random commands. Mark-to-market still scans positions, and bar processing scans orders.

The worker saves the latest complete snapshot (including retry decisions), economic digest, sequence,
chain head and SHA-256 snapshot hash in `ledger_checkpoints`. The checkpoint commits in the same
transaction as events, command responses and projections. `QS_CHECKPOINT_INTERVAL` / `--checkpoint-interval`
defaults to 10,000 events; the checkpoint is taken at the first completed batch at or beyond that
interval, so a batch can overshoot it. Set 0 to disable new checkpoints. Only the latest snapshot is
retained; the full event log remains available.

Startup verifies snapshot version/hash, restored-state invariants/digest and the checkpoint's ledger
anchor. It replays only events after the checkpoint, verifies their sequence/hash chain, then compares
the final sequence, head and digest with `portfolio_state`. Without a checkpoint it replays from genesis;
corruption fails closed. Cached totals are excluded from the economic digest for compatibility with
existing ledgers, but included in the checkpoint hash and checked against positions/orders.

A checkpoint is a trusted summary of its prefix. Startup does not re-read historical event payloads
before it. `/v1/verify` deliberately ignores checkpoints and replays the whole log to audit that prefix.
These hashes detect corruption; they do not authenticate data against someone who can rewrite the DB
and recompute hashes. Checkpoint loading/validation still grows with live state and retained retry decisions.

## Determinism contract

Deterministic: everything downstream of the frozen dataset + frozen proposals (grid search, validation,
allocation, held-out test, every ledger transition). Inputs that are *not* deterministic (LLM text,
wall-clock arrival order of concurrent HTTP requests) are fixed by recording them: proposals in the
manifest, arrival order in `commands.seq` / `ledger_events`. Replaying the recorded command log reproduces
the ledger bit for bit (`tests/recovery`, `/v1/verify`).

Equal-timestamp ordering (I-11): bars sorted by `(ts, symbol)`; at each timestamp all bars apply first
(fills for older orders), then strategies decide in `strategy_id` order with symbols ascending.

## Technology choices

- **Postgres as the queue** (commands + `FOR UPDATE SKIP LOCKED` + LISTEN/NOTIFY) instead of Kafka/Redis:
  one durable store gives atomic "consume command + write ledger" without two-phase commit.
- **Integer micro-dollars and whole shares**; rounding is always against the trader (buy fills round up,
  sell fills round down) and documented in `INVARIANTS.md`.
- **TimescaleDB hypertables** for time-series (fills, equity, bars, telemetry); relational tables for
  everything that needs uniqueness or foreign keys.
- **LangGraph** for fan-out/fan-in of the research agents; nodes after `fit` are plain deterministic Python.

## Scaling path (and what breaks first)

Measured in `PERFORMANCE.md`. Incremental agent exposure and checkpoint recovery are implemented.
The next limits are: (1) the single writer and I/O per portfolio (shard independent portfolios with
`--portfolio`); (2) position marking, order scans for fills, and full-state invariant checks/digest work
per batch (profile, then add symbol indexes/incremental marks); (3) checkpoint size and unbounded command,
event and retry histories (partition/archive with an explicit idempotency retention policy). If one
portfolio needs substantially more throughput, evaluate an in-memory single writer with its own WAL
and use Postgres for projections. Any such change must preserve the crash and full-replay proofs.
