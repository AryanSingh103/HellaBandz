# HellaBandz explained

HellaBandz is a paper-trading platform: AI agents propose strategies, a deterministic engine
simulates their trades, and an independent auditor commits evidence to Solana Devnet.
The marketplace lets demo users buy and resell strategy licenses with virtual credits.

## What the agents do

Six researchers explore different approaches: momentum, breakout, mean reversion, volatility,
RSI reversion and time-series trend. They see training data only and return validated parameter
grids for whitelisted strategies. Optional OpenAI calls produce proposals; recorded fixtures
and explicit fallbacks keep the project usable offline.

Python runs the researchers in parallel, then follows a fixed pipeline:

1. Fit candidates on training data with the shared backtester.
2. Evaluate chronological validation folds against the passive core.
3. Reject candidates without sufficient fills or evidence of out-of-sample skill.
4. Bound the supervisor's suggested allocations with deterministic caps and cash reserves.
5. Freeze the selection, evaluate held-out data once, and save a hashed manifest.

Capital that strategies have not earned stays in the passive core. The supervisor cannot
bypass execution risk rules. A live paper session turns a frozen selection into API commands;
orders fill against later historical bars, never against the bar that produced their signal.

## Where simulated trades happen

The API writes commands to PostgreSQL/TimescaleDB. One execution worker per portfolio checks
risk and writes events, fills, orders, cash and positions in an atomic transaction. No real
brokerage orders or funds are involved. Manual order tickets use the same execution path.

The system is distributed across the browser, API, manager, execution workers, database and
auditor processes. They communicate through HTTP/WebSockets and durable database records.
The API can accept queued work while an execution worker is unavailable; the worker recovers
from a verified checkpoint and replays subsequent events when restarted.

## What the UI shows

- **Markets:** frozen research strategies and their evidence.
- **Marketplace:** comparisons, virtual-credit purchases and resale listings.
- **Portfolio / Orders:** simulated holdings, cash, risk decisions and execution history.
- **Research / Run:** saved manifests, agent proposals and managed research/session jobs.
- **Audit / Analytics:** independent replay, ledger-derived statistics and blockchain receipts.

The Nike Review Radar is an alternative-data research lab. It classifies dated customer
reviews using an optional Claude/OpenAI provider or an offline rating baseline. It preserves
source context and cutoff dates. It is unbacktested and does not claim to have predicted a
historical Nike crash or submit trades. Voice questions use recorded platform facts.

## Understanding the statistics and proofs

Return measures equity change after simulated fees. Sharpe compares average returns with
variability under the engine's documented annualization convention. Maximum drawdown is the
largest peak-to-trough equity decline. Compare results from the same dataset and time period;
none guarantees future performance.

Ledger hashes expose altered events. Replay independently reconstructs accounting state.
The auditor also seals covered database records and their change journal with a root committed
to a finalized Solana Devnet transaction. This makes covered history tamper-evident; it does not
make trades execute on-chain or turn Devnet into permanent storage.

For setup and the track's ten questions, see [README](README.md). For a presentation, use
[JUDGE_DEMO](docs/JUDGE_DEMO.md). Technical boundaries live in
[ARCHITECTURE](docs/ARCHITECTURE.md), [SECURITY](docs/SECURITY.md) and
[LIMITATIONS](docs/LIMITATIONS.md). Historical measurements and submission gates are recorded
in [PERFORMANCE](docs/PERFORMANCE.md) and [SUBMISSION](docs/SUBMISSION.md).
