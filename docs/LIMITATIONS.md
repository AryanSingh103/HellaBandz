# Limitations and next steps

What follows separates **demonstrated** behavior (tests and benchmarks in this repo) from **assumptions**.

## Simulation and market realism (assumptions)

- **Bar-based fills are hypothetical.** OHLCV bars do not show queue position, hidden liquidity, or the real
  book. Fills at the next open ± fixed slippage, capped at 1% of bar volume, are a documented model, not a
  replay of executions. Limit orders fill whenever the bar trades through the limit (optimistic: no queue).
- **Synthetic default data.** The committed dataset is generated. Tournament results on it say nothing about
  real-market edge. The Alpaca path exists for real equities; we have not published results from it.
- **Small, fixed universe** (5 symbols): survivorship bias and no cross-sectional breadth.
- **No corporate actions in the engine.** It relies on split/dividend-adjusted input (`adjustment=all`).
- **No holidays or DST** in the synthetic calendar; the Alpaca script keeps only regular-hours bars.
- **Long-only cash account.** No margin, shorting, borrow, or overnight financing.
- **"Winner" means best on validation among a handful of candidates under these assumptions**, not a strategy
  with predictive power. The held-out period is evaluated once and reported even when negative (it is negative
  on the default fixture).

## System (demonstrated limits)

- **Single writer per portfolio.** All commands for one portfolio serialize through one worker. That is the
  correctness design, and it is also the throughput ceiling for one portfolio (see `PERFORMANCE.md`).
- **Remaining state scans.** Per-agent exposure and buy reservations are incremental; risk checks no longer
  scan working orders. Position marking remains O(positions), bar fills scan orders, and invariant checks,
  projection serialization and economic hashing still scan state per batch. Next: profile these paths and
  consider symbol indexes and incremental marks.
- **Checkpoint recovery trusts the saved prefix.** Every 10,000 events (at a completed batch boundary), a
  complete snapshot + digest + snapshot hash commits with the ledger. Startup verifies it and replays only
  the suffix; legacy ledgers without a checkpoint still replay in full. `/v1/verify` remains a full-history
  audit and detects altered prefix events that checkpoint startup does not inspect. SHA-256 is not an
  authentication boundary against a writer able to alter the DB and recompute hashes.
- **History and snapshot growth.** Events and commands are retained indefinitely; snapshots include every
  order retry decision to preserve idempotency across restarts. Checkpoints bound replay work, not total
  recovery memory or snapshot validation time. Next: archive/partition history and define retry retention.
- **A poison command halts the portfolio.** If a command ever made the engine violate an invariant, the
  transaction rolls back and the worker retries forever instead of committing bad state. That is fail-safe,
  not self-healing; an operator must intervene.
- **Ordering across concurrent clients** is the order in which commands were inserted (`commands.seq`), and the
  ledger records the processed order. A lower `seq` that commits late can be processed after a higher one;
  replay uses the recorded order, so determinism holds, but "fairness" between simultaneous clients is not
  defined beyond that.
- **No authentication** on the API. It is a local demo; agents are identified by `agent_id` only.

## AI components

- LLM outputs are nondeterministic. Reproducibility starts at the recorded proposals in the manifest, and
  re-running the research step will give different proposals.
- Without `OPENAI_API_KEY`, agents use recorded fixture proposals and a deterministic supervisor/crisis policy;
  manifests mark this with `source: fixture` / `fallback`.
- `/v1/ask` is read-only and grounded in database rows. Voice uses the browser's speech recognition, and
  ElevenLabs TTS when a key is configured.

## Sponsor integrations

- Solana: hash commitment via SPL Memo on **Devnet** only. Committing needs a funded Devnet key; the public
  faucet often rate-limits airdrops (the script prints the address to fund at faucet.solana.com). Offline
  verification works without the network.
- Tiger Data: runs on local TimescaleDB 2.17 (pg16); the same schema works on Tiger Cloud by changing
  `DATABASE_URL`, but that has not been benchmarked.
