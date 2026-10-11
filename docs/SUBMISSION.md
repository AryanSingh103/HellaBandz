# Submission status

The historical P1/P2/P3 combined checkout
`45e51e701ea64d7fc3bdf2898a179c34278bcf6e` was verified on October 10, 2026.
This is not yet a claim that the HackRU submission is complete.

## Current cleanup (October 11)

The `p3-core` working tree removes the retired external account/exchange integrations, dead
interfaces and graph dependencies. Six parallel research agents, simulated execution, the
strategy marketplace, Nike review lab and Solana audit trail remain; voice Q&A was removed. UI requests now
follow the active view, charts and performance helpers are shared, and dataset validation
uses a file-version cache.

The cleaned production frontend and four backend services were rebuilt and restarted. All eight
UI views loaded. Main-portfolio replay verified **2,177 events**; the browser's independent
blockchain check verified **9,603 sealed records**, with zero later changes awaiting a seal.
The judge database, marketplace holdings, wallet and public receipts were retained. Four
obsolete databases were archived to checksummed, fully readable custom dumps before removal;
three empty retired-provider tables were dropped. Old build folders and legacy bytecode were
moved to a recoverable temporary folder outside the repo.

Focused validation of the cache and research changes passed **22 tests**. This cleanup does
not rerun the full proof or benchmark suites. The measured headlines below and in
[PERFORMANCE](PERFORMANCE.md) remain historical; fresh submission-commit measurements,
judge access and Devpost publication are still separate submission gates.

## Historical combined verification (October 10)

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

- **Current performance evidence:** the complete baseline predates authentication and the full-trace
  journal. The security-build rerun was stopped to prioritize functional verification; run `make bench`
  before submitting current throughput claims. Partial runs are not a completed report.
- **Judge access:** the repository is private. The authenticated collaborator has push access but no
  admin/maintain access. The owner must make it public or grant judges access.
- **Devpost:** [DEVPOST.md](DEVPOST.md) contains prepared copy; publication/submission has not been verified.

**Solana funding resolved:** the official faucet supplied 0.5 free Devnet SOL. New commitments for
`T20261010203045`, `T20261010203316`, and `T20261010203916` were confirmed and independently
read back; API receipts and the browser verification button now work. See [VERIFICATION.md](VERIFICATION.md).
This integration remains outside the deterministic execution path.

Tiger Cloud and an Anchor registry remain optional/stretch work.
Operator API authentication and periodic full-trace Devnet seals are implemented; see [SECURITY](SECURITY.md).
Combined measurements are recorded in [PERFORMANCE.md](PERFORMANCE.md), with matching Devpost
headlines. Run the required final `make prove` and `make bench` on the chosen submission commit. Runtime
changes require fresh measurements. Do not mark the overall submission complete until judge access and Devpost status are verified.

## Reproduction

Follow the README setup, then run `make prove` and `make bench`. For the large real-data benchmarks,
first run `uv run python scripts/ingest_binance.py`; downloads are checksum-verified and excluded from
timing. Without it, the committed synthetic fixture works offline and unavailable larger sizes are disclosed.
Performance results must share the current source hash. Runtime changes after measurement require another
benchmark run; documentation-only changes do not change that hash.
