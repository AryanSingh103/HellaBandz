# HellaBandz

Private accounts and Kalshi connections: follow [Accounts and Kalshi setup](docs/ACCOUNTS_AND_KALSHI.md) before deployment. Authentication defaults to locked until Supabase is configured. Research uses virtual capital; manual demo exchange orders are optional and production connections are read-only.

**Autonomous AI strategy agents on top of trading infrastructure they cannot break.**
HackRU Fall 2026 · Lucera Trading Systems Challenge (also: Tiger Data, Solana)

LLM agents research markets, propose strategies, compete in a tournament scored on out-of-sample data, and
trade a shared $10,000 paper portfolio. All of that is untrusted input. A separate, deterministic,
single-writer execution worker decides what actually happens to money. It enforces risk limits, simulates
fills, and keeps a hash-chained ledger that survives duplicate requests, concurrent clients, malformed input,
and `kill -9`.

> **New to trading or to this project?** Start with [HELLABANDZ_EXPLAINED.md](HELLABANDZ_EXPLAINED.md): concepts, pipeline, and the track scorecard.

> Paper trading only. No real orders, no real funds, no brokerage connection. Market data is synthetic
> (committed fixture) or historical (Alpaca / Binance scripts).

## The 10 questions (Lucera starter-guide outline)

| | |
|---|---|
| **User and problem** | A desk letting autonomous agents propose trades needs a hard boundary that keeps a buggy or overconfident agent from overspending, double-filling, shorting, or corrupting the books. |
| **Core claim** | No sequence of duplicated, concurrent, malformed, delayed, or crash-interrupted commands can create cash or shares from nothing or apply a fill twice, and the ledger can always be rebuilt bit-for-bit from its event log. |
| **Boundary** | Bar-based simulated fills (no order book or queue position), long-only cash account, 5 symbols, no auth. See [LIMITATIONS](docs/LIMITATIONS.md). |
| **Correctness** | 11 invariants (idempotency, cash/position safety, risk enforcement, fill validity, determinism, accounting identity, recovery, causality, audit integrity, ordering): [INVARIANTS](docs/INVARIANTS.md). |
| **Evidence** | 92 passing tests on the combined branch: unit, property-based (random command programs checked after every step), real-Postgres concurrency, SIGKILL fault injection, tamper detection. `make prove` prints the invariant table. |
| **Interfaces** | HTTP API ⇄ Postgres command log ⇄ execution worker; agents and UI are API clients. [ARCHITECTURE](docs/ARCHITECTURE.md). |
| **Measurement** | Risk-check latency, durable order throughput at 1/10/50 clients, replay speed on 131k real Binance bars, recovery time vs ledger size, all under fixed container limits: [PERFORMANCE](docs/PERFORMANCE.md). |
| **Failure behavior** | Invalid ⇒ 422 or recorded rejection with rule code. Duplicate ⇒ original decision. Same id, new payload ⇒ 409. Stale or future signal ⇒ R7. Out-of-order bar ⇒ rejected, no effect. Worker down ⇒ commands queue (HTTP 202) and drain in order on restart. Corrupted checkpoint, suffix or projection ⇒ worker refuses to start; full-history verification audits the saved prefix. |
| **Reproduction** | Commands below, no API keys needed (committed dataset + recorded agent proposals). |
| **Next limit** | Incremental agent exposure and recovery checkpoints are implemented. Next: shard portfolios, profile position/fill/projection scans, and archive growing event and retry histories. |

## Quickstart

The terminal also includes a [strategy marketplace and Nike Review Radar](docs/MARKETPLACE.md):
compare frozen returns, Sharpe and maximum drawdown; buy and resell strategy licenses
using persistent demo-credit wallets. The Nike lab accepts dated customer reviews and
supports an optional Claude/OpenAI text classifier or an offline rating baseline.
It is labeled unbacktested, with source context separated from review evidence.

Requirements: Docker (Desktop or Colima, running), [uv](https://docs.astral.sh/uv/), Node 20+, GNU make.
On Windows install make with `scoop install make` or `choco install make`; the targets work from PowerShell or Git Bash.

```bash
make setup        # uv sync + npm ci
make up           # TimescaleDB + execution worker + API + terminal, resource-limited containers
make tournament   # agents → fit on train → rank on validation → allocate → held-out test (15–80 s)
make session      # replay held-out period through API→worker with a market crash at 50% (≈90 s)
```

The trading terminal is at http://localhost:3000 as soon as `make up` finishes (API on :8000). The first
`make up` builds three images and takes a few minutes.

A session can be replayed once per ledger, because bars are idempotent and ordered. To run it again:

```bash
make reset up tournament session   # wipes the database volume, then the full pipeline
```

Then:

```bash
make prove                       # all tests + invariant evidence table (needs `make up` for DB tests; ≈2.5 min)
make prove-docker                # the same inside a Linux container; use this on Windows
make crash-worker                # SIGKILL the worker mid-session; orders keep queueing
make restart-worker              # it replays the hash chain, verifies the digest, resumes; prints /v1/verify
make bench                       # benchmarks + regenerate docs/PERFORMANCE.md
make solana-commit verify-solana # optional: manifest hash → Solana Devnet memo, then verify
```

Optional keys in `.env` (see `.env.example` and [INTEGRATIONS](docs/INTEGRATIONS.md); `make check-integrations` shows status): `OPENAI_API_KEY` (live LLM agents instead of recorded
proposals), `ELEVENLABS_API_KEY` (spoken answers), `ALPACA_API_KEY_ID/SECRET` (real equity bars).

## Demo script (5 minutes)

The example strategy numbers below describe the recorded-fixture run. With `OPENAI_API_KEY` configured,
proposals, allocations and results vary; use the values shown by that run rather than quoting these examples.

**Before you present** (about 5 minutes, not part of the demo): `make reset up tournament`, open
http://localhost:3000, and run `make prove` (or `make prove-docker`) in a second terminal so the invariant
table is already on screen. Keep one terminal free for the commands below. The header should show two
green dots and the Portfolio panel should say the portfolio is not open yet.

| Time | Step | What to do and say |
|---|---|---|
| 0:00 | **Tournament** | Point at the Strategy tournament panel. Mean reversion has the best *training* Sharpe (1.84) but fails validation (score 0, no capital). Momentum gets the 40% cap, volatility 37.9%, the rest stays in cash. The held-out test ran once, after the freeze, and is reported even though it is negative (−8.1%). |
| 0:45 | **Live session** | Run `make session` (takes ≈90 s). Bars flow through the API, agents trade, the equity curve, order decisions and event inspector update live. |
| 1:30 | **Crisis** | About 45 s in, a −16% crash hits (Agent channel shows it). Momentum tries to triple its exposure ("buy the dip"); the risk engine reduces two orders and rejects one (R1/R2 rule codes in the channel and in Order decisions). Volatility de-risks and is approved. |
| 2:15 | **Adversarial console** | The session prints `verify: True`. Click *Same order ×100*: 1 new command, 99 duplicates, one effect. *Oversized order* is cut to what the allocation allows (R2). *Stale-data*, *Unallocated agent* and *Short sale* are rejected with rule codes. *Kill switch ON*, then *Oversized order* again: rejected (R9). Turn the kill switch OFF. |
| 3:15 | **Chaos** | `make crash-worker`. Within ≈6 s the header shows "execution worker DOWN". Click *Stale-data order*: after 5 s it returns HTTP 202 queued and "pending commands" goes up. `make restart-worker` prints the replay check (`"match": true`); the pill turns green and pending returns to 0. Press *Replay & verify*: digests match, chain intact. |
| 4:15 | **Evidence** | Show the `make prove` table from the other terminal (92 tests, 11 invariants PROVEN), then `docs/PERFORMANCE.md`. If there is time: an "Ask" question (spoken if the browser allows it), or `make verify-solana`. |

Rehearsed timings on a laptop: tournament 76 s, session 88 s (crash at 44 s), worker shown as down 6 s after
the kill, restart and replay under 5 s. The crash can also be done *during* the session: the session slows
down while commands queue, then finishes on the same final equity and ledger digest as an uninterrupted run.

If something goes wrong on stage: `docker compose ps` should list `db`, `worker`, `api`, `web`;
`docker compose logs worker` shows the replay line; `make reset up tournament` gives a clean start in about 2 minutes.

## Repository map

```
hellabandz/
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

**Built during HackRU:** everything under `hellabandz/`, `apps/web/`, `tests/`, `benchmarks/`, `scripts/`,
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
[DATA_SOURCES](docs/DATA_SOURCES.md) · [INTEGRATIONS](docs/INTEGRATIONS.md) · [LIMITATIONS](docs/LIMITATIONS.md) ·
[STRATEGY_EVALUATION](docs/STRATEGY_EVALUATION.md) · [PLAN](PLAN.md)
