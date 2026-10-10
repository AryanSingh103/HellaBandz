# Submission status

P1, P2 and P3 implementation work is combined on `main` and `p1-demo`. The combined checkout
`45e51e701ea64d7fc3bdf2898a179c34278bcf6e` was verified on October 10, 2026.
This is not yet a claim that the HackRU submission is complete.

## Combined verification

- `make setup` and `make up` passed; API, worker, database and production terminal started.
- `make prove` on Windows and `make prove-docker` on Linux each passed **92 tests**, with zero
  failures/skips and all 11 invariant categories passing.
- Live OpenAI research, supervisor, crisis responses and `/v1/ask` passed; ElevenLabs smoke and
  gateway `/v1/speak` returned audio. The WebSocket stream delivered events.
- Alpaca SIP ingestion downloaded 32,865 bars with no validation warnings. Live-LLM tournaments
  and 350-step sessions ran on synthetic and real equity data; both session replay digests matched.
- The demo worker was killed mid-session: five commands queued while it was down, restart recovered
  the ledger, the session finished and pending commands returned to zero.
- Browser checks passed for concurrent retries, oversized orders, stale data, unallocated agents,
  short sales, kill-switch enforcement and independent replay. No browser console errors were observed.
- The existing Solana commitment for `T20261010194733` was read back from Devnet and verified.
- `make bench` completed all engine, durable and recovery workloads. All 12 durable runs and eight
  recovery comparisons matched replay, and all result files share the combined source hash.

See [VERIFICATION.md](VERIFICATION.md) for exact run records. Local credentials and wallet keys are
gitignored. Raw Alpaca/Binance snapshots are not redistributed.

## Remaining gates

- **Judge access:** the repository is private. The authenticated collaborator has push access but no
  admin/maintain access. The owner must make it public or grant judges access.
- **Devpost:** [DEVPOST.md](DEVPOST.md) contains prepared copy; publication/submission has not been verified.
- **New Solana commitments:** the local Devnet wallet reports zero balance. The automatic airdrop failed;
  an existing on-chain commitment verifies, but the new tournament hashes have not been committed.
  A new commitment requires funded Devnet test SOL. It is outside the deterministic execution path.

Tiger Cloud, an Anchor registry, Kalshi and API authentication remain optional/stretch work.
Combined measurements are recorded in [PERFORMANCE.md](PERFORMANCE.md), with matching Devpost
headlines. Run the required final `make prove` and `make bench` on the chosen submission commit. Runtime
changes require fresh measurements. Do not mark the overall submission complete until judge access and Devpost status are verified.

## Reproduction

Follow the README setup, then run `make prove` and `make bench`. For the large real-data benchmarks,
first run `uv run python scripts/ingest_binance.py`; downloads are checksum-verified and excluded from
timing. Without it, the committed synthetic fixture works offline and unavailable larger sizes are disclosed.
Performance results must share the current source hash. Runtime changes after measurement require another
benchmark run; documentation-only changes do not change that hash.
