# QuantSwarm

**Autonomous AI strategy agents on top of trading infrastructure they cannot break.**
HackRU Fall 2026 · Lucera Trading Systems Challenge (also: Tiger Data, Solana)

LLM agents research markets, propose strategies, compete in a tournament scored on out-of-sample data, and
trade a shared $10,000 paper portfolio. All of that is untrusted input. A separate, deterministic,
single-writer execution worker decides what actually happens to money. It enforces risk limits, simulates
fills, and keeps a hash-chained ledger that survives duplicate requests, concurrent clients, malformed input,
and `kill -9`.

> Paper trading only. No real orders, no real funds, no brokerage connection. Market data is synthetic
> (committed fixture) or historical (Alpaca / Binance scripts).

## The 10 questions (Lucera starter-guide outline)

| | |
|---|---|
| **User and problem** | A desk letting autonomous agents propose trades needs a hard boundary that keeps a buggy or overconfident agent from overspending, double-filling, shorting, or corrupting the books. |
| **Core claim** | No sequence of duplicated, concurrent, malformed, delayed, or crash-interrupted commands can create cash or shares from nothing or apply a fill twice, and the ledger can always be rebuilt bit-for-bit from its event log. |
| **Boundary** | Bar-based simulated fills (no order book or queue position), long-only cash account, 5 symbols, no auth. See [LIMITATIONS](docs/LIMITATIONS.md). |
| **Correctness** | 11 invariants (idempotency, cash/position safety, risk enforcement, fill validity, determinism, accounting identity, recovery, causality, audit integrity, ordering): [INVARIANTS](docs/INVARIANTS.md). |
| **Evidence** | 60+ tests: unit, property-based (random command programs checked after every step), real-Postgres concurrency, SIGKILL fault injection, tamper detection. `make prove` prints the invariant table. |
| **Interfaces** | HTTP API ⇄ Postgres command log ⇄ execution worker; agents and UI are API clients. [ARCHITECTURE](docs/ARCHITECTURE.md). |
| **Measurement** | Risk-check latency, durable order throughput at 1/10/50 clients, replay speed on 131k real Binance bars, recovery time vs ledger size, all under fixed container limits: [PERFORMANCE](docs/PERFORMANCE.md). |
| **Failure behavior** | Invalid ⇒ 422 or recorded rejection with rule code. Duplicate ⇒ original decision. Same id, new payload ⇒ 409. Stale or future signal ⇒ R7. Out-of-order bar ⇒ rejected, no effect. Worker down ⇒ commands queue (HTTP 202) and drain in order on restart. Corrupted log ⇒ worker refuses to start. |
| **Reproduction** | Five commands below, no API keys needed (committed dataset + recorded agent proposals). |
| **Next limit** | One writer per portfolio caps that portfolio's order rate. Next: shard portfolios, incremental exposure sums, recovery checkpoints. |

## Quickstart

Requirements: Docker (or Colima), [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
make setup        # uv sync + npm install
make up           # TimescaleDB + execution worker + API, resource-limited containers
make tournament   # agents → fit on train → rank on validation → allocate → held-out test (≈15 s)
make session      # replay held-out period through API→worker with a market crash at 50%
make web          # trading terminal: http://localhost:3000
```

Then:

```bash
make prove                       # all tests + invariant evidence table (needs `make up` for DB tests)
make crash-worker                # SIGKILL the worker mid-session; orders keep queueing
make restart-worker              # it replays the hash chain, verifies the digest, resumes; prints /v1/verify
make bench                       # benchmarks + regenerate docs/PERFORMANCE.md
make solana-commit verify-solana # optional: manifest hash → Solana Devnet memo, then verify
```

Optional keys in `.env` (see `.env.example`): `OPENAI_API_KEY` (live LLM agents instead of recorded
proposals), `ELEVENLABS_API_KEY` (spoken answers), `ALPACA_API_KEY_ID/SECRET` (real equity bars).

## Demo script (5 minutes)

1. **Tournament.** `make tournament`: three agents propose strategies. Mean reversion has the best *training*
   Sharpe but fails validation (score 0, no capital). Momentum and volatility pass and are capped at 40%
   each, with 10% kept as cash. The held-out test runs once, after the freeze, and is reported even though it is negative.
2. **Live session.** `make session` with the terminal open: bars flow through the API, agents trade, the
   equity curve and event inspector update live.
3. **Crisis.** At 50% a −16% crash hits. The momentum agent tries to triple its exposure ("buy the dip"); the risk
   engine cuts or rejects it (R1/R2, visible with rule codes). The volatility agent de-risks and gets approved.
4. **Adversarial console.** Same order sent 100× concurrently ⇒ one effect. Oversized, stale-data,
   unallocated-agent and short-sale orders ⇒ rejected with reasons. Kill switch blocks buys.
5. **Chaos.** `make crash-worker` during the session. The API returns `202 queued`. `make restart-worker` ⇒
   replay + digest match ⇒ processing resumes with no duplicate fills. Press "Replay & verify" in the UI.
6. **Evidence.** `make prove`, then `docs/PERFORMANCE.md`; optionally `make verify-solana` and an "Ask"
   question by voice.

## Repository map

```
quantswarm/
  contracts/      versioned Pydantic schemas (orders, bars, commands, proposals, specs)
  ledger/         ★ pure deterministic core: state, events, risk rules, fill model, invariants, replay
  execution/      worker (single writer) + Postgres/Timescale schema
  api/            FastAPI gateway: idempotent command intake, read models, verify, WebSocket stream
  backtester/     DataProvider / Strategy / ExecutionModel / Portfolio / ResultStore over the same core
  strategies/     whitelisted causal strategy families (agents pick parameters, never code)
  ingestion/      snapshot format, validation, content hashing, synthetic generator
  agents/         LangGraph tournament, live session runner, crisis responses, grounded Q&A
  audit/          Solana Devnet memo commitment + verifier
apps/web/         Next.js trading terminal
tests/            unit · property · integration (real Postgres) · recovery (SIGKILL)
benchmarks/       workload generator, runners, report → docs/PERFORMANCE.md
scripts/          fixture generation, Alpaca/Binance ingestion, prove.py
data/fixtures/    committed synthetic dataset + recorded agent proposals
data/manifests/   tournament manifests (hash-committed)
```

## What we built vs. what we used

**Built during HackRU:** everything under `quantswarm/`, `apps/web/`, `tests/`, `benchmarks/`, `scripts/`,
the synthetic dataset generator, the schema, the docs, and the recorded fixture proposals.

**External components:**

| Component | Use |
|---|---|
| PostgreSQL 16 + TimescaleDB 2.17 (Tiger Data) | command log, ledger, projections, hypertables |
| FastAPI, Uvicorn, Pydantic v2, psycopg 3 / psycopg_pool | API, validation, DB access |
| LangGraph, OpenAI Python SDK (optional at runtime) | agent orchestration and LLM calls |
| NumPy, PyArrow | indicators, Parquet snapshots |
| pytest, Hypothesis | tests, property-based testing |
| httpx, aiohttp | HTTP clients (aiohttp for load generation) |
| solders + Solana JSON-RPC, SPL Memo program (Devnet) | manifest commitments |
| ElevenLabs TTS API (optional), browser Web Speech API | voice |
| Next.js 15, React 19 | trading terminal |
| Data: Binance public archive (data.binance.vision), Alpaca Market Data API (script), synthetic generator | see [DATA_SOURCES](docs/DATA_SOURCES.md) |

## Docs

[ARCHITECTURE](docs/ARCHITECTURE.md) · [INVARIANTS](docs/INVARIANTS.md) · [PERFORMANCE](docs/PERFORMANCE.md) ·
[DATA_SOURCES](docs/DATA_SOURCES.md) · [LIMITATIONS](docs/LIMITATIONS.md) · [PLAN](PLAN.md)
