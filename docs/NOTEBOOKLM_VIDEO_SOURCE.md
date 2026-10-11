# QuantSwarm: Source Document for a Video Explanation

> **How to use this file with NotebookLM**
> 1. Create a new notebook and upload this file as a source. Optionally also upload `QUANTSWARM_EXPLAINED.md`.
> 2. Open **Studio → Video Overview → Customize** and paste the prompt from Part A.
> 3. Generate. Part B is the content NotebookLM will draw from.

---

## Part A: Prompt to paste into NotebookLM's "Customize" box

```
Make a 5-7 minute explainer video about QuantSwarm for a smart audience that knows nothing about
trading or finance. Structure it as a story:
1) Hook: AI agents are starting to take real actions like moving money. What stops a confident but
   wrong AI from wrecking the books?
2) The analogy: QuantSwarm is a tiny trading firm in software. AI "researchers" invent strategies,
   but a strict "risk desk" and "back office" that the AI cannot override decide what happens to money.
3) Explain only the trading terms you need, each with an everyday example: order, fill, slippage,
   backtest, look-ahead bias, overfitting, train/validation/test, Sharpe ratio, drawdown, idempotency.
4) Walk through the pipeline in order: data → fingerprint → split time → AI agents research → tune
   → rank on unseen data → allocate $10,000 → test once → seal results on Solana → live replay →
   market crash → kill the server → recover and verify.
5) The core promise: no duplicate, simultaneous, malformed or crash-interrupted order can create or
   lose money, and the books can always be rebuilt and checked. Explain how it is proven (tests that
   attack it on purpose) and measured (benchmarks under fixed resource limits).
6) Real results, stated honestly, including the limitations.
7) Close on the big idea: "The AI proposes, the engine disposes, and the engine can prove it."
Tone: clear, confident, a little playful, never hypey. Emphasize that everything is paper trading
with fake money. Do not present any result as investment advice or proof of future profit.
Use simple diagrams: the trust boundary, the pipeline, the life of one order, a hash chain.
```

---

## Part B: The source content

### 1. The one-sentence idea

QuantSwarm lets AI agents act like traders, but every order passes through strict, deterministic
infrastructure that the AI cannot override, and that infrastructure can prove it never lost, duplicated or
invented a single cent. **The AI proposes, the engine disposes, and the engine can prove it.**

Everything is paper trading: $10,000 of pretend money, no real broker, no real orders.

### 2. Why this matters (the hook)

AI agents are moving from *answering questions* to *taking actions*: booking travel, moving money,
placing trades. The scary failures aren't about whether the AI is smart. They're about plumbing:

- The network hiccups, the agent retries, and **the same order gets placed twice.**
- **Three agents spend the same shared cash at the same instant.**
- An agent sends **nonsense**: buy minus five shares, a stock that doesn't exist, a decision based on last week's price.
- The server **crashes halfway** through recording a trade.
- Someone **edits the records** afterwards to make results look better.
- A strategy looks brilliant in testing because it was **secretly peeking at future prices.**

QuantSwarm is built so none of these can corrupt the books, and it ships automated tests that try each one
on purpose. It was built for the **Lucera Trading Systems Challenge at HackRU Fall 2026**, which asks for
"simple, reliable, and scalable" trading infrastructure with "explicit correctness guarantees and
evidence that they hold."

### 3. The analogy: a tiny trading firm in software

| Real trading firm | QuantSwarm | Can it be trusted? |
|---|---|---|
| Researchers who invent strategies | Three AI agents (GPT-4.1-mini): Momentum, Mean Reversion, Volatility | No: creative but fallible |
| Portfolio manager | An AI supervisor whose advice is clamped by a fixed formula | Advice only |
| Risk and compliance desk | The risk engine: ten hard-coded rules | Yes: deterministic |
| The exchange | A fill simulator that decides prices and quantities | Yes: deterministic |
| Back office accounting | A ledger kept in whole micro-dollars, every entry fingerprinted | Yes: deterministic |
| External auditor | Independent replay, plus fingerprints published on the Solana blockchain | Verifiable by anyone |
| Trading screen | A live web terminal | Watches only |

The key design idea is the **trust boundary**. Everything AI-related lives outside it and can only reach the
money through one narrow, validated door.

### 4. The handful of trading terms you need

- **Stock / ticker:** a share of a company. We trade five: SPY (top 500 US companies), QQQ (big tech), Apple, Microsoft, NVIDIA.
- **Price bar:** a one-hour summary of trading: opening price, high, low, closing price and volume.
- **Order:** "buy 10 shares of Apple." A **market order** says "at whatever the price is"; a **limit order** says "only at $190 or better."
- **Fill:** the order actually happening. A **partial fill** means only some shares were bought.
- **Slippage:** you expected $100 and paid $100.05. Real trading always has some friction.
- **Position:** what you currently own. **Long-only** means you can't bet on prices falling by selling shares you don't own.
- **Backtest:** replaying history to see how a strategy *would have* done.
- **Look-ahead bias:** accidentally letting a strategy see the future, like grading a student who peeked at the answer key. It makes results look amazing and fake.
- **Overfitting:** tuning so perfectly to the past that it fails on anything new, like memorizing last year's exam answers.
- **Train / validation / test:** split history into three periods: tune on one, compare on a second, and take a final exam on the third exactly once.
- **Sharpe ratio:** return divided by how bumpy the ride was. Above 1 is good; negative means losing.
- **Drawdown:** the worst drop from a peak. "At one point we were down 12% from our best."
- **Idempotency:** sending the same request twice has the same effect as sending it once. This is the cure for accidental double orders.
- **Hash / fingerprint:** a short code computed from data. Change one character and the fingerprint changes completely.

### 5. The pipeline, as a story

**Step 1: Get the data.** Real US stock prices from Alpaca (hourly, January 2023 to September 2026), real
Bitcoin prices from Binance for speed testing, and a synthetic dataset committed to the project so anyone can
run it with no accounts.

**Step 2: Check it and fingerprint it.** No duplicate timestamps, no impossible prices, no missing values. The
cleaned file gets a SHA-256 fingerprint; if a single byte changes later, the system refuses to load it.

**Step 3: Split time.** The first 60% of history is for training, the next 20% for validation, and the last 20%
is the sealed final exam.

**Step 4: The AI agents research.** Three agents work in parallel. Each sees only *training-period
statistics*, never the validation or test periods. Each must answer in strict JSON: which approved strategy
family, which stocks, which settings to try, and why. The AI can only choose from four pre-approved strategy
families. It cannot write code. Bad answers get one retry with the error explained, then a safe fallback.

**Step 5: Tune.** A plain, repeatable search tries every combination of settings the agent suggested on the
training period and keeps the best. No AI involved, so this step gives the same answer every time.

**Step 6: Judge on unseen data.** Each tuned strategy is tested on the validation period it has never seen,
plus three extra slices of history for stability. Pass/fail gates: at least 10 trades, and a drawdown no worse
than 25%. **This is where overfitters get caught.** On the synthetic data, the Mean Reversion agent had the
best training score (Sharpe 1.84) but collapsed on validation (−0.03), and received zero dollars.

**Step 7: Hand out the $10,000.** The AI supervisor suggests weights. A fixed formula decides: proportional to
score, the AI can nudge each weight by at most 20 percentage points, no strategy gets more than 40%, 10% always
stays in cash, and failed strategies get nothing whatever the AI says.

**Step 8: Freeze, then take the final exam once.** Only after everything is locked is the test period run,
exactly once, and reported honestly even when the result is negative.

**Step 9: Seal it on Solana.** The full record (data fingerprint, AI proposals, settings, scores,
allocations, results) is fingerprinted and the fingerprint is written to the Solana blockchain (Devnet, free
test network). Anyone can later recompute it from the file. Change one number and it no longer matches. It's
like sealing your predictions in an envelope before the game.

**Step 10: Go "live."** The test period is replayed through the real running system as if it were a live
market feed. The frozen strategies turn prices into orders, and every order travels the full production path.
The session also deliberately re-sends some orders to imitate a flaky network.

**Step 11: Crash the market.** Halfway through, prices drop 16% in four hours, trading volume triples, and
costs widen. Each AI agent is asked how to react: buy the dip, cut back, or hold. Their answers become ordinary
orders, and the risk engine approves, shrinks or rejects each one, with a rule code explaining why.

**Step 12: Kill the server.** A judge force-kills the execution engine mid-session. Orders keep arriving and
are safely queued. On restart, the engine rebuilds its books from the event history, checks every
fingerprint, and continues. Nothing is lost and nothing is duplicated.

**Step 13: Ask it questions.** "Why was that order rejected?" "Which agent did best?" Type or speak. Answers
are built from real database records, and ElevenLabs reads them aloud. This path can't change anything.

### 6. The life of one order

1. An agent asks: buy 10 shares of Apple, with a unique order ID.
2. The front door (API) checks the request is well-formed and writes it to a durable to-do list. If the same
   ID arrives again with the same content, it's recognized as a retry and gets the original answer. If it
   arrives with *different* content, it's refused as a conflict. Either way, the order can never execute twice.
3. The execution engine, the only component allowed to touch money, checks ten risk rules: the portfolio is
   open; no single order above 25% of equity; each agent stays inside its allocation; total investment at most
   90%; enough free cash; no selling what you don't own; sensible quantity and price; decision based on fresh
   data, not stale or future; same ID means same answer; and an emergency kill switch that trips automatically
   at a 15% loss.
4. Cash is reserved so no other agent can spend it.
5. The trade fills on the *next* hour's price, never the hour the decision was made on (no peeking), with
   realistic friction, limited to 1% of that hour's volume, and never beyond the reserved cash. A sudden price
   jump means a partial fill, never debt.
6. Everything is saved in one all-or-nothing database transaction: the event, the reply, the new balances and
   a new fingerprint. A crash at any moment leaves either the whole trade or none of it.
7. Before saving, a checker confirms the books still balance. If they wouldn't, nothing is saved.

Money is stored as whole micro-dollars (integers), never as decimals that can drift (computers famously compute
0.1 + 0.2 as 0.30000000000000004). Rounding always goes against the trader, which is the conservative choice.

### 7. The promises, and how they are proven

QuantSwarm states eleven "invariants": things that must always be true.

1. Sending an order a hundred times is the same as sending it once.
2. Cash never goes negative, and agents can't double-spend shared cash.
3. You can never own a negative number of shares.
4. No order skips the risk rules: not from the AI, not from the screen.
5. Trades only happen at prices and sizes the market data allows.
6. The same inputs always produce exactly the same trades and balances.
7. The accounting identity always balances to the micro-dollar.
8. A crash loses nothing and duplicates nothing, and corrupted records are detected.
9. Strategies can never see the future.
10. Editing data, records or results is detected.
11. Simultaneous events are always processed in the same documented order.

**How they're proven:** 92 automated tests, all passing, including:
- **Random attack sequences:** the computer invents hundreds of hostile sequences of up to 150 commands
  (giant orders, negative quantities, retries, conflicting retries, wrong-agent cancels, 30% price gaps) and
  checks every promise after every single step.
- **Mutation check:** the team deliberately broke the engine twice, and the tests caught both bugs.
- **100 identical orders fired at once** produce exactly one trade.
- **50 oversized orders at once** from five agents, through a 40% price jump, never overdraw the cash.
- **Repeated force-kills** of the engine under load, after which the books equal a from-scratch recomputation.
- **Tamper tests** on the data file, the event history, the stored balances and the published results.
- **No-peeking tests:** poisoning every future price changes nothing about past decisions.

### 8. How it is measured

Everything runs in containers with fixed CPU and memory limits (database 2 CPUs, engine 1 CPU, API 1 CPU), and
each measurement reports environment, workload, metric, procedure and interpretation, the format Lucera asks
for. Results on an Apple M3 laptop:
- A risk check takes about **3 microseconds**.
- The full durable path (web request, database, engine, saved trade, reply) handled about **1,000 orders per
  second** with 50 simultaneous clients, and under 4 milliseconds per order for a single client.
- Replaying **131,040 real Bitcoin price bars** ran at about **50,000 bars per second**.
- Recovering a ledger of **100,000 events** took about **1.6 seconds**; recovery checkpoints keep this fast as
  history grows.

The team also documented what went wrong while measuring: a database notification feature that secretly
serialized everything; hashing that got slower as the ledger grew; and a load-testing tool that turned out to
be measuring itself instead of the system. All three were fixed. Lucera explicitly values this kind of honesty.

### 9. Real results (stated carefully)

**Live AI agents on real stock data** (Alpaca, five large US stocks, hourly bars 2023–2026): all three GPT
agents' strategies passed validation and received capital (37%, 30% and 23%). On the sealed final exam,
January to September 2026, evaluated once, the frozen portfolio returned **+6.3%** with a Sharpe ratio of 1.10
and a worst drop of 5.5%. That's one period, five stocks and a simplified fill model, so it's a demonstration
of the process, **not evidence of a money-making edge.**

**The crash, with live AI:** two agents chose to buy the dip and one chose to cut back. The risk engine approved
the sells, approved some buys, shrank one and rejected two for exceeding the agents' allocations. Afterwards,
an independent replay of 300 simulated hours matched the books exactly.

**On-chain:** the tournament's fingerprint is recorded in a Solana Devnet transaction. Verification passes, and
changing one allocation weight by 1% makes it fail.

### 10. The technology, briefly

- **Python** for the engine, **FastAPI** for the front door, **TimescaleDB / Tiger Data** (Postgres for time
  series) as the durable to-do list, the ledger and the analytics.
- **LangGraph + OpenAI** to run the three research agents in parallel and the supervisor.
- **Next.js** for the live trading terminal.
- **Solana** (Devnet, SPL Memo) for public, tamper-evident fingerprints.
- **ElevenLabs** for spoken answers. **Alpaca** and **Binance** for real market data.
- Everything optional works offline: with no keys, the system uses recorded agent proposals and synthetic data.

### 11. Honest limitations

- Fills are simulated from hourly price bars; the system doesn't see a real order book.
- Five large stocks is a small, biased universe. Long-only, cash only: no shorting, no borrowing.
- One engine per portfolio is deliberately the throughput ceiling for that portfolio; scaling means more
  portfolios and more engines, not more locks.
- AI answers aren't repeatable, so reproducibility starts from the *recorded* AI proposals in the sealed manifest.
- Operator authentication protects the workspace; marketplace identities remain shared demo labels.

### 12. The closing idea

The interesting part of QuantSwarm isn't whether an AI can pick stocks. It's the **boundary**: a small, boring,
provably correct system that lets creative but unreliable AI agents act, without ever letting them break the
rules or the books. **The AI proposes. The engine disposes. And the engine can prove it.**

Built at HackRU Fall 2026 for the Lucera Trading Systems Challenge, with integrations for Tiger Data, Solana,
OpenAI, ElevenLabs and Alpaca. Paper trading only.
