# Understanding HellaBandz

A guide for the team: what we built, how it answers the Lucera track, and how to present it.
It assumes little or no trading background. Every claim points at code, a test or a command.

---

## 1. The short answer: what is this project?

**HellaBandz is a backtesting engine and a paper-trading system. AI agents sit on top of it, and
they are not allowed to break it.**

"Backtracking" in our notes means **backtesting**: replaying old market prices to check how a
trading strategy *would have* performed. We do that. We also do more:

| Layer | What it does | Is it the core? |
|---|---|---|
| **The engine (ledger + worker)** | Holds the pretend money. Checks every order against risk rules, simulates fills and keeps tamper-evident books. | **Yes. This is the product.** |
| **Backtester** | Runs strategies over historical prices using that same engine. | Yes. This is starter-guide idea #4. |
| **AI research agents** | Several agents each suggest a strategy and its settings. Plain code tests the suggestions, keeps the ones that earned it and freezes them. | Built on top. |
| **Coordinated trading** | Several strategies share one account. A coordinator merges their wishes into one order per stock, so they never trade against each other or spend the same dollar twice. | Built on top. This is the "collab" part. |
| **Paper exchange (CLOB)** | A small internal stock exchange with an order book. Orders can be matched here instead of filled from price bars. | Built on top. |
| **Extras** | Strategy marketplace, Nike review research lab, Solana audit seal, voice Q&A. | No. Don't lead with these. |

**So is it backtesting or collaboration?** Both, stacked:

1. **Research** (backtesting): agents propose, code tests on old data, the winners get frozen.
2. **Trading** (paper trading, with collaboration): the frozen strategies trade a replay of newer
   data together, through the API, the database and the worker, with a coordinator between them.

Underneath both is one deterministic engine. A backtest and a live paper session use **the same
code** to decide what an order costs, so the two cannot disagree.

**The one sentence to remember.** AI agents are untrusted input. A single, deterministic execution
worker decides what happens to money, and we prove it cannot be broken by duplicate, concurrent,
malformed or crash-interrupted requests.

---

## 2. Trading crash course (only what you need)

Read this once and the rest of the doc will make sense.

**Market data**
- **Bar (OHLCV):** a summary of one time slice, for example one hour of AAPL. It records the
  Open, High, Low and Close prices, plus Volume (shares traded). We use hourly bars for 5 stocks:
  SPY, QQQ, AAPL, MSFT and NVDA.
- **Snapshot:** a frozen file of bars with a hash, so everyone tests on byte-identical data.
  There is a committed synthetic dataset (no accounts needed) and a real Alpaca dataset.

**Orders**
- **Order:** "buy 10 AAPL". A **market order** means "at whatever the price is". A **limit order**
  means "only at $132.54 or better".
- **Fill:** the order actually executed. A **partial fill** means only some of the shares did.
- **Slippage:** you rarely get the exact price you saw. We charge 5 basis points (0.05%) against
  you, and 50 during the scripted crash.
- **Fee:** $0.005 per share.

**Accounts and accounting**
- **Position:** how many shares you hold. **Long-only** means you can only own shares, never
  bet against them (no "shorting"). That is our rule.
- **Cash reservation:** when you place a buy, we set aside the cash right away. Two orders can
  then never spend the same dollar.
- **Ledger:** the books. Ours is a list of events where each event contains the hash of the one
  before it (a "hash chain"), so editing history is detectable.
- **P&L (profit and loss):** *realized* means locked in by selling. *Unrealized* means paper gains
  on shares still held.
- **Equity:** cash plus the current value of holdings.

**Measuring results**
- **Drawdown:** the drop from a peak. The kill switch trips automatically at 15%.
- **Sharpe ratio:** return divided by how bumpy it was. Higher is better.
- **Buy-and-hold / passive core:** "just buy all 5 stocks and wait". It is the bar any strategy
  must beat. If a strategy can't beat it, our system leaves the money in buy-and-hold.

**Backtesting terms**
- **Look-ahead bias:** the classic backtest bug, where the strategy accidentally uses tomorrow's
  price to decide today. We test that this is impossible.
- **Train / validation / test split:** like studying, a practice exam and the real exam. Tune on
  train, choose on validation, and look at test **once**, at the end. Looking at test more than
  once is cheating, so we count every look.

**Exchange terms**
- **Order book / CLOB:** a central limit order book, the list of all waiting buy and sell orders
  at each price. A **matching engine** pairs buyers with sellers.
- **Price-time priority:** the best price goes first. At equal prices, the earliest order goes
  first. This is how real exchanges are fair.
- **Liquidity provider:** someone always willing to buy or sell. Ours is simulated.
- **Netting:** if strategy A wants to sell 4 AAPL and strategy B wants to buy 6, don't send two
  orders. Move 4 internally and buy only 2 on the market. That saves fees and avoids trading
  against yourself.

**System terms**
- **Kill switch:** an emergency stop. It blocks new buys and still allows sells.
- **Paper trading:** fake money, realistic mechanics. **Nothing here touches real money.**
- **Idempotency:** sending the same request twice has the same effect as sending it once. This
  matters because networks retry.

---

## 3. The app in one picture

```
                         UNTRUSTED (can be wrong, slow, random)
  ┌───────────────────────────────────────────────────────────────────────────┐
  │  AI research agents (OpenAI, or recorded answers offline)                 │
  │    each picks a strategy family + settings from a fixed menu              │
  │  → code: grid-search on TRAIN, rank on VALIDATION vs buy-and-hold         │
  │  → code: cap allocations, test ONCE on held-out data, freeze + hash       │
  │                                                                           │
  │  Session runner / coordinated runtime                                     │
  │    replays newer bars, turns strategy signals into ordinary HTTP orders   │
  └──────────────────────────────────┬────────────────────────────────────────┘
                                     │ HTTP
  ┌──────────────────────────────────▼────────────────────────────────────────┐
  │  API (FastAPI)  validates input, writes each command ONCE to Postgres     │
  │                 retry → same answer · same id + new content → 409         │
  │                 worker down → 202 "queued", nothing lost                  │
  └──────────────────────────────────┬────────────────────────────────────────┘
                                     │ Postgres "commands" table = ordered queue
  ┌──────────────────────────────────▼────────────────────────────────────────┐
  │  Execution worker   THE ONLY THING THAT CAN CHANGE MONEY                  │
  │    one per portfolio (database lock), takes commands in order             │
  │    risk rules R0–R9 → fill model → invariant check                        │
  │    → ONE transaction: events + balances + response                        │
  │    on restart: replay the log, verify hashes, refuse to start if wrong    │
  └──────────────────────────────────┬────────────────────────────────────────┘
                                     │
                     TimescaleDB  ←→  Next.js trading terminal (browser)
                                  ←→  Auditor (seals record hashes on Solana Devnet)
```

Running services (`docker-compose.yml`): `db`, `worker`, `api`, `manager` (runs research and
session jobs), `web` and `auditor`. These are separate processes that talk over HTTP, WebSockets
and the database.

### Life of one order, step by step

1. A strategy, or you in the UI, sends `buy 10 AAPL, id=abc`.
2. The API inserts it into `commands` with `ON CONFLICT DO NOTHING`. If `abc` already exists, you
   get the original answer back. If `abc` exists with *different* contents, you get a 409.
3. The worker picks it up in sequence order and runs the risk rules:

   | Rule | Meaning |
   |---|---|
   | R1 | Max 25% of equity per order |
   | R2 | Each agent stays inside its own budget |
   | R3 | Max 90% invested overall |
   | R4 | Must have the cash |
   | R5 | Can't sell what you don't own |
   | R6 | Input must be sane |
   | R7 | Signal can't be from the future or stale |
   | R8 | Duplicate handling |
   | R9 | Kill switch |

   An order that is too big is **reduced**, not rejected, and the event records which rule cut it.
4. Cash is reserved right away.
5. The order fills on the **next** bar, never the bar that triggered it. It fills at that bar's
   open plus slippage, and at most 1% of the bar's volume. Leftovers expire after 5 bars and give
   their cash back.
6. Events, balances, the response and a state fingerprint all commit in **one** database
   transaction. Either all of it happens or none of it does.

Code: `hellabandz/api/app.py`, `hellabandz/execution/worker.py`, `hellabandz/ledger/engine.py`,
`hellabandz/ledger/state.py`.

### Life of one coordinated cycle (the "collab" part)

Code is in `hellabandz/trading/`. The full spec is in `docs/COORDINATED_TRADING.md`.

1. **Market-data gateway** publishes the new bar. It drops duplicates, buffers out-of-order
   events and never hands strategies data they could modify.
2. Three strategies (trend, mean reversion, defensive) each emit an **intent**, such as "I'd like
   +4 lots of AAPL". Intents are not orders.
3. **Coordinator** merges all intents per stock into **at most one order**. It nets opposite wishes
   internally, applies budget limits and records why each intent was accepted, reduced, netted,
   deferred or rejected. Example output from a real run:
   > *"2 agents requested a change in AAPL (defensive +2, mean_reversion +4 lots); it approved
   > one aggregate BUY of 6 at a limit of 132.54 within the portfolio budget."*
4. **Execution authority** sends that one order to a venue. The venue is either the bar
   simulator or the internal **CLOB**, a real price-time-priority matching engine against a
   simulated liquidity provider.
5. **Attribution** splits the single account back into per-strategy "sleeves" and checks that the
   sleeves still add up to the ledger after every cycle. The demo reports `breaks=0`.

---

## 4. Requirement 1: "Address a trading or financial-market infrastructure use case"

**The use case.** A trading desk wants to let autonomous strategies, including AI-driven ones,
propose trades. It needs a hard boundary so that a buggy or overconfident strategy cannot:

- overspend,
- double-fill an order,
- sell shares it doesn't have,
- trade against its own sibling strategies, or
- corrupt the books.

It also needs a backtester whose results it can trust: no peeking at the future, identical
results on every rerun, and books that always balance.

That is infrastructure, not a strategy. The track itself lists several of the pieces we built:

| Track's example direction | Ours | Where |
|---|---|---|
| Backtesting engine that prevents look-ahead bias and supports reproducible runs | Yes, central | `hellabandz/backtester/`, `hellabandz/strategies/` |
| API-driven backtesting engine | Yes, `POST /v1/jobs` starts research and sessions | `hellabandz/api/workflows.py` |
| Order book / matching engine | Yes, internal CLOB with price-time priority | `hellabandz/trading/clob.py` |
| Paper-trading venue | Yes | the whole worker path |
| Pre-trade risk checks, position limits, kill switch | Yes, R0–R9 | `hellabandz/ledger/engine.py` |
| Event-sourced system / audit trail / reconciliation | Yes: hash-chained events, replay, CLOB-vs-ledger reconcile, Solana seal | `ledger/state.py`, `audit/` |
| Market-data ingestion / replay | Yes: gateway, snapshots, Alpaca and Binance ingest scripts | `trading/marketdata.py`, `ingestion/`, `scripts/` |
| Execution simulator | Yes, documented bar fill model | `ledger/engine.py` (`EngineConfig`) |
| Connectivity with realistic failure handling | Partly: write-ahead journal, unknown-outcome handling, rate limit. **Not wired to a real exchange.** | `trading/external.py` |

Execution is fully simulated. Data is synthetic, or historical from Alpaca or Binance. This is
what the track asks for.

---

## 5. Requirement 2: "Demonstrate a working implementation"

For systems, the track says: *a working end-to-end path where at least two components
communicate as part of the core workflow, and each separation serves a real purpose.*

**The end-to-end path:**

```
make up           # db + worker + api + manager + web + auditor, resource-limited containers
make tournament   # agents → train → validation → allocate → held-out test → frozen manifest
make session      # replay the held-out period through API → Postgres → worker, crash at 50%
```

Then open http://localhost:3000 and watch orders, fills and balances update.

**Why each boundary exists** (judges will ask):

| Boundary | Purpose |
|---|---|
| API ↔ worker (separate processes, Postgres queue between them) | The worker can crash or restart without losing a single request. Commands wait in the queue (HTTP 202) and drain in order. Show this with `make crash-worker` and `make restart-worker`. |
| One worker per portfolio | One writer means no race conditions on money, without complex distributed locks. |
| Agents/UI ↔ API | Untrusted, nondeterministic code (LLMs, browsers) can only *ask*. It never touches state directly. |
| Manager | Long research jobs don't block the API. |
| Auditor | The only process holding the Solana signing key. It is isolated so a compromised API can't forge seals. |

**Verified today (2026-10-11):**
- `uv run pytest -q tests/unit tests/property`: **175 passed** in about 10 s.
- `uv run python -m hellabandz.trading.demo` ran 1,368 coordinated cycles in both venue modes
  with 0 reconciliation breaks.
- Not re-run today: the integration and recovery suites need Docker (`make up`, then
  `make prove`).

---

## 6. Requirement 3: "State at least one important correctness property and test it"

### The headline claim

> No sequence of duplicated, concurrent, malformed, delayed or crash-interrupted commands can
> create cash or shares from nothing, overdraw cash, short a stock or apply a fill twice. The
> ledger can always be rebuilt bit-for-bit from its event log.

We state 12 invariants in `docs/INVARIANTS.md`. These are the five worth knowing by heart:

| ID | In plain words | How the tests try to break it |
|---|---|---|
| **I-01 Idempotency** | Sending an order twice never fills it twice. | 100 *concurrent* HTTP retries of one order against real Postgres must produce exactly one effect. Same id with a new payload must return 409, never overwrite. |
| **I-02 Cash safety** | You can never spend cash you don't have, even if the price jumps. | 50 concurrent oversized orders plus a 40% price gap. A gap-up must produce a partial fill, never debt. |
| **I-07 Accounting identity** | `cash + cost of holdings = starting cash + realized profit − fees`, exactly, in integers. | Checked after **every** batch in the real worker, where a violation rolls the transaction back. A property test throws up to 150 random commands per case and checks after every one. |
| **I-08 Crash recovery** | Killing the worker at any moment loses nothing and duplicates nothing. | Real `SIGKILL` before and after commit, repeated random kills under load, tampered projections (the worker must refuse to start). |
| **I-09 No look-ahead** | A strategy never sees the future. | Cut the data at 25 random points and check earlier decisions never change. Fill future data with garbage and check the result is identical. |

The others cover risk enforcement (I-04), fill validity (I-05), determinism (I-06), audit
integrity (I-10), ordering (I-11) and operator access (I-12).

**The property test.** `tests/property/test_ledger_properties.py` uses Hypothesis to generate
random sequences of buys, sells, retries, bad quantities, wrong-agent cancels, kill-switch
toggles and ±30% price gaps. As a sanity check, the team ran it against two deliberately broken
engines. It caught both. That is the best evidence that the tests have teeth.

**Run it:** `make prove` runs everything and prints which test backs which invariant (needs
`make up` first, about 2.5 minutes).

**If a judge asks "which invariant matters most?"** Say **idempotency together with the
accounting identity**. Networks retry and processes crash. If a retried or replayed order can
fill twice, money is created from nothing, and every other guarantee is meaningless.

---

## 7. The four qualities from the track intro

> "Systems that must communicate quickly, preserve correctness, handle concurrent activity, and
> remain understandable as they scale."

### Communicates quickly

The two halves have very different costs. Say so.

| Path | Measured |
|---|---|
| Pre-trade risk check, in memory | 1.8 µs median, 3.1 µs p99 |
| Full engine step (risk + event + hash) | 17 µs median, about 35k commands/s on one thread |
| Durable order (HTTP → Postgres → worker → commit → reply) | 9 ms for 1 client. **482 orders/s** at 50 clients (p99 168 ms). |
| Coordinated cycle, 2 vCPU / 1 GiB container | 0.6 ms median (bar sim), 1.9 ms (paper CLOB) |
| Live coordinated session, ledger round trip | 4.5 ms median, 14.2 ms p99 |
| Crash recovery, 105,000-event ledger | 0.44 s using a checkpoint |
| Backtest replay | about 38k–57k bars/s |

Sources: `docs/PERFORMANCE.md` (fixed container limits are written down) and
`docs/COORDINATED_TRADING.md`.

**The story:** the engine adds microseconds. Nearly all the milliseconds go to I/O, meaning the
HTTP hop and the database commit. Under load the worker groups many commands into one
transaction, which is why throughput rises with more clients.

**Honest caveat.** `docs/PERFORMANCE.md` labels its tables as a historical baseline, taken before
operator auth and the audit journal were added. Run `make bench` before quoting them as current.

### Preserves correctness

See section 6. Three design choices make it possible:

- **Integer money.** Amounts are stored in millionths of a dollar, so no float drift. Rounding
  always goes against the trader.
- **A pure engine.** `process(state, command) → events` has no clocks, no randomness and a fixed
  iteration order. That makes replay exact.
- **One transaction per batch.** There is never a half-written state.

### Handles concurrent activity

- **Many clients, one writer.** Any number of agents and browsers can hit the API at once.
  Postgres assigns each command a sequence number, and one worker per portfolio processes them in
  that order. The worker holds `pg_advisory_lock`, so a second worker can't start
  (`test_second_worker_blocks_on_single_writer_lock`).
- **Duplicates under concurrency.** The `UNIQUE` constraint on `command_id` makes 100 simultaneous
  retries collapse to one.
- **Strategies competing for the same cash.** Cash reservations plus per-agent budgets (R2) stop
  overspending. In coordinated mode, the coordinator nets intents so strategies never trade
  against each other.
- **Background research while trading.** The 2-vCPU benchmark ran a parameter search in another
  process at the same time. Execution latency did not get worse.

### Remains understandable as it scales

- **One rule for who changes money:** only the worker. Everything else is a client.
- **Clear next bottleneck:** one writer per portfolio is both the correctness design and the
  throughput ceiling. To scale, **shard by portfolio** (`--portfolio`), with one worker each.
  Don't loosen ordering within a portfolio.
- **What grows without bound:** the command and event tables (fix: partition or archive) and the
  checkpoint size.
- **Explainable decisions:** every rejection carries a rule code, and every coordinator decision
  carries a sentence explaining it.

---

## 8. Do we match the starter guide's examples?

The starter guide gives four detailed ideas:

| Guide idea | Us? |
|---|---|
| 1. Python tooling for SBE (binary message encoding) | No |
| 2. FIX message explainer with bounded AI | No |
| 3. FIX log analyzer | No |
| **4. API-driven backtesting engine** | **Yes. This is our primary match.** |

Point by point against idea #4's "realistic 24-hour scope":

| Guide asks | We have |
|---|---|
| Data-provider interface, one asset class, one frequency | `DataProvider` / `SnapshotProvider`, US equities, 1-hour bars. It refuses to return bars at or after the end time. |
| Small strategy API | Whitelisted strategy families. Agents pick parameters only, never code. |
| Deterministic timestamp order | Sorted by (time, symbol). Equal timestamps follow a documented order (I-11). |
| Documented fill model | Next-bar open ± slippage, limit only if traded through, 1% volume cap, 5-bar expiry |
| Cash, positions, fees, P&L | One shared ledger engine |
| Trades, equity curve, reproducible manifest | `data/manifests/*.json` with dataset hash, config hash, proposals, picks and results |

The guide's #4 correctness ideas (no look-ahead, equal-timestamp order, reconciliation after
every fill, identical reruns) each map to a named test (section 6). We also built most of its
"useful extensions": configurable slippage and commission, leak-free parameter sweeps, event-level
replay (`/v1/replay/{seq}`), checkpointing and a REST API with immutable results.

Beyond #4, the track's broader list also covers our CLOB, risk checks, paper venue and
event-sourced audit trail (section 4).

**Where we diverge from the guide.** Its advice is "choose one core claim and prove it well." We
built a lot around the engine. Present the extras as extras.

---

## 9. The extras, and how to talk about them

| Feature | What it is | One-line framing |
|---|---|---|
| Strategy marketplace | Buy and resell strategy licenses with virtual credits. Uses atomic transfers. | "A side demo. It never touches trading cash." |
| Nike Review Radar | An AI classifies dated customer reviews as alternative data. | "A research lab. It's unbacktested and places no trades." |
| Solana Devnet seal | Hashes of 16 record sets committed on-chain. | "Makes later edits to the database detectable by an outsider. It doesn't make trades happen on-chain." |
| Voice Q&A | Answers built from database rows. The AI only rephrases them. | "Accessibility. The AI can't invent facts." |

**How the AI is fenced in** (the guide explicitly values this):

- **Research agents** fill in a form that is checked against a whitelist. One bad answer gets one
  retry. After that, a recorded fallback is used.
- **The supervisor** suggests weights. Code clamps each weight to ±20 points and a 40% cap, and it
  can't fund a strategy that failed.
- **The crisis response** during the crash still goes through R0–R9.
- **Reproducibility.** AI output isn't deterministic, so we *record* it in the manifest and replay
  the recording.

---

## 10. Our strategies don't beat buy-and-hold, and that's fine

- Coordinated evaluation on 10 fresh synthetic markets: mean **−0.75%** versus buy-and-hold.
  In 7 of them nothing passed the funding test, so the system just held buy-and-hold.
- Real Alpaca data, held out Jan–Sep 2026: **+12.42% versus +12.42%**. No strategy earned money,
  so the portfolio *was* buy-and-hold.

The guide says to *"focus on trustworthy mechanics rather than sophisticated strategy returns."*
Our system's job is to refuse to fund strategies without evidence, and it did. Say it plainly. It
is a strength, not something to hide.

One trap: on the Alpaca test period, the defensive overlay alone made +15.8%. Selection had
rejected it on train and validation. **Do not** say "we should have used it." Choosing it now
would be picking parameters with the test set, which is exactly the cheating the split exists to
prevent.

---

## 11. Presenting it in order (follows the judge review flow)

1. **Eligibility (30 s).** "Trading infrastructure: a backtester plus a paper-trading engine that
   untrusted AI strategies can't break. Simulated execution, historical or synthetic data."
2. **Demo (2 min).**
   - Open the terminal and show a frozen research run: train, validation and test results.
   - Show the portfolio, orders and fills.
   - Run `make crash-worker` mid-session. Orders keep queueing.
   - Run `make restart-worker`. The worker replays, verifies and resumes, and `/v1/verify`
     matches.
3. **Correctness (1 min).** Give the headline claim. Name idempotency plus the accounting
   identity. Describe the 100-concurrent-retries test and the SIGKILL tests. Mention that the
   property test caught two deliberately broken engines.
4. **Performance (1 min).** Microseconds in the engine, milliseconds on the durable path,
   482 orders/s at 50 clients, all under stated container limits. Every benchmark run ends with
   an independent replay that must match.
5. **Design (1 min).** One writer per portfolio is simple and correct, and it is also the first
   thing that hits its limit. The fix is sharding by portfolio. Postgres serves as the queue so
   that "consume command + write ledger" is one atomic transaction, with no Kafka and no
   two-phase commit.

### Likely questions, short answers

- **"Why not Kafka or Redis?"** One durable store gives us an atomic consume-and-write. Two
  systems would need two-phase commit or would risk double processing.
- **"Is the order book real exchange depth?"** No. It is a synthetic liquidity ladder built from
  bar data, and it is labeled as such everywhere.
- **"What happens if a live coordinated job crashes?"** The in-process CLOB's book is lost. On
  restart, its paper orders are cancelled and their reservations released, never filled. This is
  listed as not done yet.
- **"Can two strategies trade against each other?"** In coordinated mode, no: the coordinator nets
  them. In the original tournament session mode each agent has its own sub-account, so it is
  possible there. Shared risk rules still prevent overspending.

---

## 12. Command cheat sheet

```bash
make setup && make up         # install, start everything (http://localhost:3000)
make tournament               # research run
make session                  # paper-trade the held-out period, crash at 50%
make crash-worker             # kill the worker
make restart-worker           # recover + verify
make prove                    # all tests + invariant table (needs make up)
uv run pytest -q tests/unit tests/property     # fast, no Docker: 175 tests, ~10 s
uv run python -m hellabandz.trading.demo       # coordinated multi-strategy demo
make bench                    # regenerate performance numbers
make reset up tournament session   # WARNING: wipes the database volume
```

**Where to read more:** `docs/ARCHITECTURE.md`, `docs/INVARIANTS.md`, `docs/PERFORMANCE.md`,
`docs/COORDINATED_TRADING.md`, `docs/LIMITATIONS.md`.
