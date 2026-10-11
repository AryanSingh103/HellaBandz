# Coordinated multi-strategy trading

Several strategy agents share one account without spending the same money twice or trading against
each other. Strategies propose. One coordinator decides. One execution authority submits. The
existing hash-chained ledger stays the only place money moves.

Code: `hellabandz/trading/`. Ledger changes: `hellabandz/ledger/engine.py`, `hellabandz/ledger/state.py`.

## Components

| Component | Module | Job |
|---|---|---|
| Market-data gateway | `marketdata.py` | The only publisher of market events. Typed, immutable events with sequence numbers, data class (historical, synthetic, demo or production) and units. Drops duplicates, rejects conflicts, buffers out-of-order events, marks gaps for resync, applies backpressure to bars and trades (never drops them), coalesces quotes, keeps bounded history. Strategies get read-only snapshots, and every array they get is a private copy. |
| Strategy agents | `strategies.py` | Trend (SMA crossover), mean reversion (z-score) and defensive (volatility target). Each is a reviewed implementation of a whitelisted family, with a card (hypothesis, data, entry, exit, bounds, failure conditions, costs) and a version hash. Indicators update incrementally. They emit `Intent`s, never orders. No LLM runs here. |
| Coordinator | `coordinator.py` | Turns all intents for an instrument into at most one order, with a recorded reason for each intent (see below). |
| Attribution | `attribution.py` | Splits the one ledger account into strategy sleeves. Checked against the ledger after every cycle. |
| Execution authority | `execution.py` | The only order submitter. Venue adapters: historical simulation, paper CLOB, shadow. |
| Internal CLOB | `clob.py` | Deterministic single-writer matching engine. |
| Runtime | `runtime.py` | Runs the steps above once per market timestamp, on one thread, and times each stage. |
| Instruments | `instruments.py` | Separate definitions for equity/ETF, crypto and event contracts. |

## Venue modes and what a "fill" means

| Mode | Who decides fills | Ledger path | Fill label |
|---|---|---|---|
| `historical_sim` | The ledger's documented bar model: next bar open ± 5 bps, a limit fills only if the bar trades through it, at most 1% of bar volume. | `SubmitOrder`, filled on `ApplyBar` | `venue=bar_sim` |
| `paper_clob` | The internal CLOB. The counterparty is a simulated liquidity provider (`sim_lp`) quoting a 3-level ladder around each close, sized at 1% of bar volume, or another simulated participant. **This depth is synthetic, built from OHLCV bars. It is not real exchange depth.** | `SubmitVenueOrder`, then `ApplyVenueFill` per trade | `venue=paper_clob`, `liquidity=synthetic` or `internal` |
| `shadow` | Nobody. Orders are recorded only. | none | none |
| external demo / production | The external venue. Not wired to these equity strategies. Production orders stay disabled. Kalshi demo orders keep their existing separate path (`docs/ACCOUNTS_AND_KALSHI.md`) and are never fed stock strategies. | | |

A paper fill is never stored or shown as an external fill.

## How CLOB events enter the ledger

| CLOB event | Ledger command | Effect on the account |
|---|---|---|
| Our order accepted by the coordinator | `SubmitVenueOrder` (limit only) | Same R0–R9 risk checks as any order. Buys reserve `qty × (limit + fee)` in cash. Sells reserve shares. |
| `Trade` involving our order | `ApplyVenueFill(trade_id, qty, px)` | The ledger re-checks that the order exists on that venue, the quantity is still working, the price honors the limit, a buy fits its reservation, and the fill is not dated before the decision. If all pass: cash, position, basis and fees move exactly as for a bar fill, and slippage = qty × \|px − last close\|. A failed fill is recorded as a `VenueFillRejected` event, never silently dropped. Idempotent on `(venue, trade_id)`. |
| IOC remainder or self-trade cancel | `CancelOrder` | Releases the remaining reservation. |
| Ledger closes the order (kill switch) | CLOB `Cancel(reason=LEDGER_CLOSED)` | Keeps the book and the ledger in step. A reconcile pass after each cycle compares them. |

Bar-sim orders are untouched by all of this. Their payload hashes, snapshots and digests are byte
identical to before (a backtest digest was compared against `main`).

## CLOB rules

- Bid and ask books per instrument, with price-time priority.
- Limit orders only, GTC or IOC. Trades execute at the maker's price.
- Partial fills are allowed. An IOC remainder is cancelled with reason `INSUFFICIENT_LIQUIDITY`.
- Two buys never match. No liquidity is created: every trade needs a resting order someone submitted.
- Self-trade prevention: `cancel_newest` (the default) or `cancel_oldest`.
- Cancel/replace loses time priority.
- Integer ticks and lots throughout.
- Idempotent `command_id`. Order ids are never reused.
- Hash-chained events replay identically.

## Coordinator rules (in order)

1. **Kill switch on, or day loss ≥ limit:** no new buying. Exits are still allowed.
2. **Intent validation:**
   - Rejected for: duplicate id, unknown strategy, inactive version, instrument outside the
     strategy's universe, unsupported asset class (event contracts and crypto), wrong venue,
     data from the future, out of order, expired, or stale.
3. **Stale or gapped data** for an instrument: nothing is traded in it.
4. **Desired lots per strategy** = (sleeve budget for the instrument × weight) / price. Changes
   smaller than 10% of that budget are ignored unless they are a full exit. This is the same
   band the research backtests use.
5. **Internal netting.** Strategies selling give lots to strategies buying, at the last close,
   before anything goes to market. This is the arbitration rule. Scores are never compared across
   strategies, because they are not calibrated against each other.
6. **Lifecycle.** While a working order or an unacknowledged submission exists for the
   instrument, no new order is sent. A working order is kept if it is within 20% of the needed
   size, otherwise cancelled and replaced on the next cycle.
7. **Limits on net buys:**
   - per-order notional
   - committed capital
   - per-instrument and correlated-group concentration
   - gross exposure
   - cash after reservations, including earlier orders in the same cycle
   - daily turnover

   Sells are capped at holdings. A cooldown and a minimum trade size stop churn.
8. **The order.** One limit order at last close ± the tightest slippage any contributing strategy
   accepts. It carries an attribution plan.

Every decision records each intent's outcome (`accepted`, `reduced`, `netted`, `deferred` or
`rejected`) with reason codes, plus a sentence built from those records. For example: *"2 agents
requested a change in AAPL (defensive +2, mean_reversion +4 lots); it approved one aggregate BUY of
6 at a limit of 132.54 within the portfolio budget."*

Budgets: `paper_budget(equity)` for the $10,000 simulation, and `small_budget(equity)` for tiny
accounts. On a $5 account it refuses every buy, because one share costs more than the account. It
does not pretend the $10,000 numbers apply.

## Attribution rules

1. **Capital.** Each sleeve starts with its research allocation. The rest is "unallocated".
2. **Fills.** A fill is split across the order's plan in proportion to the lots each strategy
   still has planned. Integer lots are assigned by largest remainder, with ties going to the
   alphabetically first strategy id. Fees and slippage are split the same way.
3. **Buys and sells.** Buys add lots and basis. Sells remove them at the sleeve's own average
   cost and realize the difference.
4. **Internal transfers** move lots and cash between sleeves at the last close. The account does
   not change.
5. **Always exact:** lots per instrument, cash, fees, slippage and total profit.
6. **Not required to match:** the realized/unrealized split, because sleeves use their own
   average cost while the ledger pools the account. The total is the same.

## Run it

```bash
uv run python -m hellabandz.trading.demo              # deterministic demo, both venue modes
uv run pytest -q tests/unit                            # includes the CLOB, gateway, strategy, coordinator and venue-ledger tests

# 2-vCPU benchmark (the cgroup limits are recorded in the output)
docker build -t hellabandz-bench .
docker run --rm --cpus=2 --memory=1g -v "$PWD/benchmarks/results:/out" hellabandz-bench \
  python -m hellabandz.trading.demo --background-research --json /out/coordinated_2vcpu.json
```

There is nothing to migrate. The schema change is additive (`fills.venue` and `fills.liquidity`,
with defaults) and is applied by the existing startup migration.

## Measured (2 vCPU, 1 GiB, Docker on Apple Silicon)

The cgroup recorded `cpu.max 200000 100000` and `memory.max 1073741824`.

- **Workload:** the full held-out period of the synthetic fixture: 1,368 cycles, 5 instruments,
  3 strategies, in-process ledger.
- **Excluded:** simulated acknowledgements are local function calls, so no network latency is
  included or claimed.

| Stage (µs) | historical_sim p50 / p95 / p99 | paper_clob p50 / p95 / p99 | paper_clob + background research |
|---|---|---|---|
| Market data in (gateway + ledger marks; paper also requotes the LP ladder) | 121 / 189 / 257 | 1,235 / 1,599 / 2,141 | 1,192 / 1,292 / 1,512 |
| Intents (3 strategies) | 261 / 311 / 478 | 271 / 401 / 604 | 264 / 379 / 442 |
| Coordinator decision | 169 / 363 / 963 | 185 / 557 / 740 | 169 / 360 / 644 |
| Ledger submit | 30 / 46 / 75 | 35 / 51 / 66 | 34 / 45 / 58 |
| CLOB match | none | 83 / 106 / 137 | 82 / 97 / 116 |
| Whole cycle | 629 / 957 / 1,436 | 1,859 / 2,433 / 3,172 | 1,777 / 2,148 / 2,501 |
| Throughput | 1,240 cycles/s | 468 cycles/s | 495 cycles/s |
| Peak RSS | 181 MB | 277 MB | 280 MB |

- **Gateway queue:** the queue never held more than 5 events, and there were no reconciliation breaks.
- **Background research:** a parameter search ran at nice 10 in its own process during the third
  column. Execution latency did not get worse; the differences are within run-to-run noise.
- **Determinism:** both paper runs ended with the same ledger digest.
- **Bottleneck:** the liquidity provider's requote (cancel and repost 6 quotes × 5 instruments)
  is most of the paper market-data cost.

Strategy results on this run, for the record: historical_sim made +5.96% and paper_clob +4.22%,
with turnover of about 26× capital. That is high, and it is the main thing to fix before trusting
these strategies. Fixture results say nothing about real markets (`docs/LIMITATIONS.md`).

## Implemented vs not yet

**Implemented and tested:**
- CLOB
- market-data gateway
- runtime strategies, with the incremental signal matching the vectorized research definition bar for bar
- coordinator (netting, limits, lifecycle, validation)
- attribution, with a property test that sums are exact and the ledger replays
- venue orders and fills in the ledger
- execution authority (historical, paper, shadow)
- deterministic demo
- 2-vCPU benchmark

**Not done yet:**
- **Live path:** the coordinated runtime is not yet wired to the API/worker for live browser
  sessions. It runs against the in-process ledger, the same `ledger.process` the worker uses.
- **UI:** no coordinator panel yet. Decision records and explanations exist in the runtime result.
- **Promotion:** no champion/challenger registry, promotion or rollback.
- **Evaluation:** no ensemble-vs-individual-vs-baseline evaluation report, and no research critic agent.
- **External adapter:** no generic adapter with a persisted submission journal for unknown outcomes.
- **Restarts:** no restart test for the coordinated runtime's own state. Ledger restart and
  recovery are covered by the existing worker tests.
- **Turnover** is high and needs a cost-aware rebalance band.
