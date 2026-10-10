# QuantSwarm — Refined Build Plan (HackRU Fall 2026, Lucera primary)

This is the original design plan, retained for context. The implemented interfaces, risk rules and
commands are documented in `README.md`, `docs/ARCHITECTURE.md` and `docs/INVARIANTS.md`;
`MILESTONES.md` and `docs/SUBMISSION.md` track current completion. Planned details below are not
claims that every proposed feature shipped.

## Context
The team has a strong scope doc but it leaves the *mechanics* undefined: exact schemas, how a "live" session works, how fills are timed, how agents use train/val/test without leaking, how the ledger/digest/crash-recovery actually work, what the risk numbers are, and what gets cut when time runs out. This plan fixes those blanks so four people can code in parallel from hour 0 without re-deciding things. Repo `/Users/aryan/Desktop/QuantSwarm` is empty — greenfield.

Guiding rule (unchanged, now enforced structurally): **the financial core is deterministic Python with zero LLM/network dependency; AI sits strictly upstream and only emits schema-valid data.**

---

## 1. Key corrections / refinements to the original scope

1. **One accounting core, two callers.** `packages/ledger` (pure functions: apply_fill, reserve_cash, release, mark_to_market) is imported by BOTH the backtester and the execution worker. This is what makes "backtest == live sim" credible and halves the bug surface. Original doc implied this; make it a hard rule.
2. **LLMs do not trade per bar.** LLM agents pick a strategy family + parameter *ranges* + rationale. A deterministic grid search picks exact params on the train split. During a live session, orders come from the frozen strategy's signals, not from LLM calls. LLMs are only invoked at: research time, supervisor allocation, and scripted events (crash). Keeps cost low, latency out of the hot path, and runs reproducible.
3. **Split usage clarified.** Train = parameter search. Validation = ranking + allocation (this is "out-of-sample" for the tournament). Test (held-out 20%) = evaluated exactly once per tournament after allocation is frozen, reported honestly, and replayed as the "live" session. Agents never see test data or test metrics before freeze — enforced by the data access layer (`DatasetView` refuses timestamps beyond the split boundary).
4. **"Live" session = accelerated replay of the held-out period**, not a real feed. Replay service publishes bars at N× speed into `market_bars_live`; crash scenario is an overlay (e.g. −12% gap at bar k, 3× volume, widened slippage) applied to the replay stream and recorded in the run manifest. Alpaca IEX websocket is an optional "observe only" panel, never feeds the ledger.
5. **Cut Kalshi to "stretch only"; Binance to P1.** Kalshi adds a different instrument model and zero Lucera points. Binance trades are used only for the replay-throughput benchmark (100k real events) — that's its best use.
6. **Integer money.** Cash in integer micro-dollars (`int`, 1e-6 USD); quantities in whole shares (no fractional) for equities. Prices stored as `NUMERIC(18,6)` in DB, `Decimal` at boundaries, converted to int micros in the ledger. Rounding: banker's rounding documented once in `INVARIANTS.md`. Long-only cash account, no margin, no shorts.
7. **Concurrency model is explicit:** single portfolio row locked with `SELECT ... FOR UPDATE` inside the command transaction; idempotency via `UNIQUE(client_order_id)` on `commands`. No distributed locks, no Kafka. Postgres is the queue (outbox/inbox tables + `LISTEN/NOTIFY` wakeups).

---

## 2. Stack (pinned decisions)
- Python 3.12, `uv` workspace monorepo; Pydantic v2 for all contracts; FastAPI; asyncpg/psycopg3; pytest + hypothesis.
- DB: TimescaleDB (`timescale/timescaledb:2.x-pg16`) locally in Docker; mirror to Tiger Cloud free tier for the TigerData prize demo (same schema, swap `DATABASE_URL`).
- Agents: LangGraph + OpenAI (`gpt-4.1-mini`-class for agents, structured outputs / JSON schema mode). Fallback: recorded proposal fixtures in `data/fixtures/proposals/*.json` so the demo runs with no API key.
- Frontend: Next.js (App Router) + Tailwind + lightweight-charts (TradingView) for candles/equity; TS types generated from FastAPI OpenAPI via `openapi-typescript`.
- Solana: Devnet, **SPL Memo program** carrying `sha256(canonical_manifest)`; canonical JSON via RFC 8785 (JCS). Script in TS (`@solana/web3.js`) or Python (`solders`). Anchor registry = stretch.
- Voice: browser mic → ElevenLabs STT (Scribe) → API `/ask` (OpenAI with read-only tools over DB) → ElevenLabs TTS. Read-only only; no voice-triggered trades in MVP (removes confirmation-flow scope).

---

## 3. Data plan (fills the blanks)
- **Alpaca:** `GET /v2/stocks/bars`, `feed=sip` for historical (free plan allows SIP history older than 15 min), `timeframe=1Hour`, `adjustment=all`, regular hours only. Universe SPY, QQQ, AAPL, MSFT, NVDA. Range 2023-01-03 → 2026-09-30 (~6.5k bars/symbol). Fallback `1Day` if anything breaks.
- **Binance:** `BTCUSDT` 1m klines + trades for 2024-01 (with `.CHECKSUM` verification). Handle ms vs µs timestamps (2025+ spot files use µs).
- **Freeze:** each pull → normalized Parquet in `data/snapshots/<dataset_id>/` + manifest JSON `{dataset_id, source, symbols, interval, range, adjustment, row_count, sha256, schema_version, created_at}`. Loader verifies hash before use.
- **Validation checks (fail = dataset rejected):** UTC tz-aware, strictly increasing per symbol, no duplicates, OHLC sanity (low ≤ open/close ≤ high, >0), volume ≥ 0, gap report vs NYSE calendar (`exchange_calendars`). Gaps reported, not filled.
- **Splits:** chronological 60/20/20 by timestamp, boundaries stored in manifest.
- **Committed fixture:** small synthetic dataset (seeded GBM + scripted crash) in `data/fixtures/` so tests and a judge clone work without Alpaca keys. Don't commit raw Alpaca data (redistribution terms) — commit the download script + manifest hash.

---

## 4. Contracts (packages/contracts) — define in hour 0–2, then freeze
`Bar{symbol, ts, o,h,l,c, volume, dataset_id}`
`StrategyProposal{proposal_id, agent_id, family, universe[], param_ranges, rationale, evidence[], created_at, schema_version}`
`StrategySpec{strategy_id, family, params(fixed), universe, version}` (output of grid search)
`EvalResult{strategy_id, split, metrics{net_return, sharpe, max_dd, turnover, cost, n_trades}, trades_digest, equity_digest}`
`Allocation{run_id, strategy_id, agent_id, capital_micros}`
`OrderCommand{client_order_id, portfolio_id, agent_id, symbol, side, qty, type(MKT|LMT), limit_px?, signal_bar_ts}`
`RiskDecision{client_order_id, outcome(APPROVE|REDUCE|REJECT), approved_qty, rule_codes[]}`
`Fill{fill_id, client_order_id, qty, px_micros, fee_micros, bar_ts}`
`LedgerEvent{portfolio_id, seq, type, payload, prev_hash, hash}` (hash-chained)

Strategy families (whitelist, `packages/strategies`): `sma_crossover(fast,slow)`, `breakout(lookback)`, `zscore_reversion(lookback, entry_z, exit_z)`, `vol_target(lookback, target_vol)` (overlay that scales exposure). Each is a pure function `signal(history_window) -> target_weight`, receives only bars ≤ current ts.

---

## 5. Database schema (TigerData)
Hypertables: `market_bars(dataset_id, symbol, ts, …)`, `market_bars_live`, `telemetry(ts, service, metric, value)`, `equity_points(run_id, portfolio_id, ts, equity_micros)`.
Relational: `datasets`, `proposals`, `strategies`, `evaluations`, `tournament_runs`, `allocations`, `portfolios(id, cash_micros, reserved_micros, kill_switch, version)`, `positions(portfolio_id, agent_id, symbol, qty, reserved_qty)`, `commands(client_order_id UNIQUE, status, payload, received_at)`, `risk_decisions`, `orders(open/partial/filled/cancelled/expired)`, `fills`, `ledger_events(portfolio_id, seq, …, PRIMARY KEY(portfolio_id, seq))`, `checkpoints(portfolio_id, seq, state_digest)`, `outbox(id, topic, payload, published_at)`, `solana_commitments`.
Continuous aggregates: per-agent P&L by hour, rejection counts by rule.

---

## 6. Execution worker — exact semantics
**Command path (one DB transaction):** insert into `commands` (ON CONFLICT on `client_order_id` → return original result, no effect) → lock portfolio row → risk check → write `risk_decisions` → if approved: reserve cash (buy) or reserve qty (sell), create `orders` row → append `ledger_events` → write `outbox` → commit. `NOTIFY` after commit.

**Risk rules (defaults, configurable, all codes in `INVARIANTS.md`):**
- R1 max order value ≤ 25% of portfolio equity ($2,500 on $10k) → REDUCE to fit, REJECT if < 1 share
- R2 per-agent exposure ≤ agent allocation
- R3 portfolio gross exposure ≤ 90% equity (10% cash reserve)
- R4 buying power: cost + est. fee ≤ cash − reserved
- R5 sell qty ≤ position − reserved_qty (long-only)
- R6 qty integer > 0; limit price > 0 and within ±10% of last close
- R7 market data freshness: `signal_bar_ts` within 2 bars of latest bar
- R8 duplicate `client_order_id` → idempotent replay of original decision
- R9 kill switch: portfolio drawdown ≥ 15% from peak, or manual → reject all buys
- R10 per-agent order rate ≤ N per bar

**Fill model (bar-based, deterministic):** order created at bar t can only fill on bar t+1 or later.
- MKT: px = open(t+1) × (1 ± slippage_bps), slippage default 5 bps (crash overlay: 50 bps).
- LMT buy fills if low(t+1) ≤ limit at min(open, limit); sell mirror.
- Volume cap: fill qty ≤ 1% of bar volume; remainder stays open, expires after 5 bars.
- Fee: configurable, default $0.005/share, min $0 (Alpaca equity commission is $0; fee models a spread cost).
- Fills processed when the worker consumes the next bar event; same ledger transaction pattern.

**Recovery:** on start, worker loads latest checkpoint, replays `ledger_events` after it through `packages/ledger`, computes `state_digest = sha256(canonical(cash, reserved, positions sorted))`, compares to projection tables; mismatch = refuse to start (loud). Unprocessed `commands`/pending orders resume from DB status. Because every mutation + event is in one transaction, a `kill -9` at any point yields either "committed" or "never happened".

---

## 7. Agent workflow (LangGraph graph)
`load_dataset_summary(train only)` → fan-out to `momentum_agent`, `mean_reversion_agent`, `volatility_agent` (parallel, each gets train-period stats + tool `run_train_backtest(family, params)` capped at ~10 calls) → `validate_proposal` (Pydantic + whitelist; invalid → one retry with error) → `grid_search_on_train` (deterministic) → `evaluate_on_validation` (deterministic) → `critic` node (each agent sees others' validation metrics, writes critique — UI flavor, no effect on scores) → `supervisor` proposes weights + reasons → `deterministic_allocator` → persist + freeze manifest.
All LLM inputs/outputs persisted to `proposals`/`agent_messages` and streamed to UI. Tournament reproducibility claim applies from the frozen proposals onward.

**Scoring (deterministic):** gates: n_trades_val ≥ 10, max_dd_val ≤ 25%, no causality violation. Score = Sharpe_val × (1 − max_dd_val) × stability, where stability = fraction of 3 walk-forward folds inside train+val with positive net return. Score ≤ 0 → no capital.
**Allocator:** weights ∝ max(score, 0), blended with supervisor weights (50/50, supervisor can only shift within ±20pp), cap 40% per strategy, 10% cash reserve, normalize to $10,000.

---

## 8. Correctness test suite (Lucera 30%)
`tests/property` (hypothesis): ledger accounting identity (cash + Σ position×px − fees − initial = P&L), no negative cash/position under random command sequences (I-02, I-03, I-07).
`tests/integration` (real Postgres): same order ×100 concurrently → one effect (I-01); 50 concurrent oversized orders → cash never < 0 (I-02); bypass attempts (direct API, malformed, stale) (I-04); partial/limit fills (I-05).
`tests/recovery`: fault-injection hook `CRASH_AT=after_risk|after_fill_before_commit|after_commit` → subprocess killed, restarted, digest equal, no duplicate fill (I-08).
`tests/unit`: future-data test — strategy fn wrapped with a view that raises on access past ts (I-09); backtest run twice → identical trades/equity digest (I-06); tampered manifest fails verify (I-10).
`make prove` runs all and prints an invariant table — this is a demo artifact.

---

## 9. Benchmarks (Lucera 20%)
Docker Compose with `cpus: 2`, `mem_limit: 2g` on DB and worker; versions pinned; seeded workload generator.
- Risk-check latency (in-process, no DB): p50/p95/p99 over 10k proposals.
- DB-inclusive order commit throughput at 1/10/50 concurrent clients, 10k orders with 10% dupes + 5% invalid; report orders/s + p99.
- Replay speed: events/s for 10k synthetic and 100k Binance trades through fill engine.
- Recovery time: kill → ready (digest verified) for ledgers of 1k/10k/100k events.
3 warm-up + 5 measured runs, report median + spread, hardware listed. Output → `benchmarks/results/*.json` + generated `docs/PERFORMANCE.md`. Report bottlenecks honestly (likely: per-portfolio row lock serializes writes — explain why that's intentional).

---

## 10. Repo layout
Keep the original tree, with changes: add `packages/ledger/` (shared accounting core), `services/replay/` (live-session bar publisher + crash overlay; can be a module inside execution if short on time), `packages/contracts` also emits JSON Schema for the TS side. Drop `scripts/ingest_kalshi`. Add `Makefile` targets: `up`, `seed`, `tournament`, `session`, `prove`, `bench`, `verify-solana`.

---

## 11. End-to-end pipeline (what happens, in order)
1. **Ingest:** `scripts/ingest_alpaca.py` → validate → Parquet snapshot + manifest hash → load into `market_bars`.
2. **Split:** 60/20/20 boundaries fixed in manifest.
3. **Research:** 3 LangGraph agents read train-only summaries, propose family + param ranges + rationale (persisted).
4. **Fit:** deterministic grid search on train → `StrategySpec` per agent.
5. **Rank:** backtest each spec on validation (same ledger core, same fill model) → gates → score.
6. **Allocate:** supervisor suggestion + deterministic allocator → `$10k` split, frozen into tournament manifest.
7. **Commit:** manifest hash (dataset hashes, specs, val metrics, allocations) → Solana Devnet memo.
8. **Live session:** replay held-out period at N× → strategies emit target weights → convert to `OrderCommand`s → API → execution worker → risk → orders → fills on next bar → ledger events → outbox → WebSocket → UI.
9. **Crisis:** crash overlay fires → volatility agent/supervisor (LLM) propose de-risking; an aggressive agent's oversized order hits R1/R4/R9 → REJECT shown in UI with rule codes.
10. **Chaos:** judge kills execution container → orders queue in `commands` → restart → replay + digest match → processing resumes, no duplicate fills.
11. **Close:** final test-period metrics, final ledger digest appended to manifest, second Solana memo, `verify` script re-derives both hashes.
12. **Ask:** voice question → grounded answer from DB queries.

---

## 12. Team split + timeline (24h)
- **E1 Execution:** contracts (with all), `packages/ledger`, execution worker, risk, fills, recovery, invariant tests.
- **E2 Data/backtest:** ingestion + validation + snapshots, Timescale schema/migrations, backtester, replay service + crash overlay, benchmarks.
- **E3 Agents:** strategy families, grid search, scoring/allocator, LangGraph graph, proposal fixtures, ElevenLabs `/ask`.
- **E4 Frontend/API:** FastAPI gateway + WebSocket, Next.js terminal (tournament view, live session, order/risk feed, event inspector, digest panel), Solana commit/verify, Docker Compose, README.

| Hours | Milestone (gate before moving on) |
|---|---|
| 0–2 | Contracts merged, compose up (DB+api+worker), synthetic fixture generated |
| 2–6 | Manual order via curl → risk → ledger → event row. Alpaca snapshot ingested |
| 6–10 | Backtester on shared ledger, deterministic digest test green; idempotency + concurrency tests green |
| 10–14 | Agents → grid → validation → allocation end-to-end (fixtures path works without OpenAI) |
| 14–18 | Live replay session + crash + kill/restart working through UI |
| 18–21 | Benchmarks recorded, `make prove`, Solana memo + verify, voice |
| 21–24 | Feature freeze, clean-clone test, docs, Devpost, record demo video backup |

**Cut line (drop in this order if behind):** Kalshi → Binance → voice → Solana → critic node → limit orders (MKT only). Never cut: ledger, risk, idempotency, recovery, determinism tests, one benchmark, docs.

---

## 13. Docs to ship
README (5-command quickstart), ARCHITECTURE (diagram + trust boundaries), INVARIANTS (I-01..I-10 → test file mapping), DATA_SOURCES (licenses, feeds, hashes), PERFORMANCE (generated), LIMITATIONS (bar fills ≠ real liquidity, 5-symbol survivorship, IEX/SIP caveats, LLM nondeterminism upstream of freeze), dependency disclosure.

---

## 14. Lucera guideline audit (track PDF + starter guide)
Meets all 7 minimum requirements by design: trading-infra use case; ≥2 communicating components (api ↔ execution worker via Postgres command/outbox, agent worker ↔ api); invariants + automated tests; reproducible measurement; docs; dependency disclosure; simulated execution only.

Gaps found and now added to the plan:
1. **"Choose one core claim and prove it well."** Headline claim (what we say when a judge asks "which invariant matters most"): *No sequence of duplicated, concurrent, malformed, or crash-interrupted agent orders can create cash or shares out of nothing or double-apply a fill* (I-01 + I-02 + I-07 + I-08, one property test that attacks all four). Everything else is supporting.
2. **Design simplicity (25%) risk:** Solana + ElevenLabs + Kalshi add boundaries a judge will question. Keep them strictly outside the core path, behind flags, and say so in ARCHITECTURE.md. Core = 3 processes + Postgres.
3. **Use the starter guide's interface names** in `packages/backtester`: `DataProvider`, `Strategy`, `ExecutionModel`, `Portfolio`, `ResultStore`.
4. **Equal-timestamp ordering invariant (new I-11):** events with equal ts processed in order (ts, symbol asc, event_type rank: bar < signal < order < fill, seq). Test: shuffled input → same digest.
5. **Failure behavior for delayed / out-of-order / missing input:** bars arriving out of order are rejected with reason; stale signals rejected by R7; missing bars → no fill on that bar, logged; delayed commands (worker down) processed in arrival order on restart. Each has a test.
6. **PERFORMANCE.md follows Lucera's 5-part template exactly:** Environment / Workload / Metric / Procedure / Interpretation (+ next bottleneck). LLM latency reported separately, never mixed into engine numbers. Data download time separate from simulation time.
7. **Disclosure section** in README: "Built during HackRU" vs "external" table.
8. **LIMITATIONS.md + "Next limit"** section: single portfolio row lock caps writes per portfolio; next step = shard by portfolio / move ledger to single-writer in-memory engine with WAL.
9. **Starter guide's 10-question outline** answered in README top section (user, core claim, boundary, correctness, evidence, interfaces, measurement, failure behavior, reproduction, next limit).

## 15. Open decisions for the team (defaults chosen; change if you disagree)
- OpenAI model + who pays (default: mini model, budget cap ~$10, fixtures fallback).
- Tiger Cloud vs local only (default: local for dev, cloud mirror for demo).
- Solana script language (default: TS, reuse in Next.js for the verify button).
- Strict 1-hour bars vs daily (default 1h; fallback daily if Alpaca entitlements fail at hour 2).

## Verification (definition of done)
From a clean clone: `make up && make seed && make tournament && make session && make prove && make bench` all succeed without OpenAI/Alpaca keys (fixtures path), UI shows tournament → allocation → live session → rejection → kill/restart with equal digests, `make verify-solana` passes and fails after editing the manifest, and `docs/PERFORMANCE.md` contains measured numbers.
