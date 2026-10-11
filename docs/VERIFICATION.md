# Combined branch verification

Verified October 10, 2026 on `p1-demo` checkout `45e51e701ea64d7fc3bdf2898a179c34278bcf6e`.
Runtime/benchmark source SHA-256:
`29f61f2c8c1b4c1510ee8653bea8c67c30b400817f5deaf2e333540cb9738890`.
This opening record describes that earlier combined checkout. Follow-up sections record
subsequent runtime changes; use their source hashes and evidence for those versions.

## Setup and correctness

`make setup`, `make up`, `make check-integrations`, `make prove` and `make prove-docker` completed.
Windows tests: **92 passed in 131.37 s**. Linux-container tests: **92 passed in 58.08 s**.
Both runs had zero failures/skips and all 11 invariant categories passing with real TimescaleDB.
These are separate complete runs, not 184 distinct tests.

The production terminal returned HTTP 200. Browser checks showed the tournament, portfolio, equity
curve, risk decisions, live events and successful full replay. The adversarial console returned:

| Action | Observed result |
|---|---|
| Same order x100 concurrently | One new command, 99 duplicates, one economic effect |
| Oversized buy | Reduced from 100,000 to 2 shares by R2 |
| Day-old signal | Rejected by R7 |
| Unallocated agent | Rejected by R2 |
| Short-sale attempt | Rejected by R5 |
| Buy with kill switch on | Rejected by R9; switch turned off afterward |
| Replay and verify | Digests match; chain intact |

No browser console errors were observed. The browser tab is left open at http://localhost:3000.

## Live integrations and sessions

OpenAI smoke returned JSON from `gpt-4.1-mini-2025-04-14`. All three research proposals and supervisor
responses in both new tournaments used the live LLM. Crisis responses also used the LLM. Gateway
`/v1/ask` returned `mode: llm`; `/v1/speak` returned 77,784 audio bytes, and the WebSocket stream delivered
an events message. The direct ElevenLabs smoke returned 29,301 audio bytes.

Alpaca SIP ingestion: **32,865 bars**, content hash beginning `733b700631cb9167`, no validation warnings.
Both SIP and IEX smoke requests succeeded. The raw snapshot remains gitignored.

| Run | Dataset | Held-out test return | Session | Replay |
|---|---|---|---|---|
| [T20261010203045](../data/manifests/T20261010203045.json) | Synthetic fixture, live LLM proposals | -2.18% | 350 steps, 1,750 bars, 177 order requests, 8 duplicate retries | Match, 2,167 events |
| [T20261010203316](../data/manifests/T20261010203316.json) | Alpaca SIP equities, live LLM proposals | +3.96% | 350 steps, 1,750 bars, 162 order requests, 11 duplicate retries | Match, 2,163 events |

Held-out returns belong to the tournament backtest. The shorter sessions include a scripted market
crash and are different workloads; their final equities were $9,773.72 and $9,380.38 respectively.
These results do not establish a durable trading edge.

The synthetic session was interrupted with `make crash-worker`. Health showed `worker_alive: false`
and five pending commands. `make restart-worker` recovered, the session completed in 87.56 s, and
the final full replay matched with zero pending commands. The real-data session ran in its own
portfolio and completed in 154.67 s with a matching digest. Their summaries are committed beside
their tournament manifests. Later manual console checks add events beyond these session snapshots.

The existing `T20261010194733.solana.json` commitment verified against the actual Devnet memo.
The new local wallet was created under ignored `data/solana/`; it still reported zero SOL after
the automatic airdrop failed. This funding blocker was resolved in the later check below; no private key is committed.

## Solana follow-up (October 10, 2026)

The Solana Foundation web faucet supplied 0.5 free Devnet SOL to the ignored local wallet.
Three new SPL Memo commitments were confirmed, read back from Devnet, and published to the API:
`T20261010203045`, `T20261010203316`, and `T20261010203916`. Their public receipts are stored
beside the manifests as `.solana.json` files. The previously committed `T20261010194733` receipt
was reused and published without sending a duplicate transaction.

The API now verifies and retains the signature, including on signature-free research retries.
The Research page's verification button checked the newly created `T20261010203916` memo against
the recomputed canonical manifest hash and displayed a successful match. The production web build
and 31 focused Solana/integration unit tests passed. The complete Linux proof run then passed
127/127 tests with all 11 invariant categories proven and no skips. Solana Explorer independently
displayed the new `T20261010203916` transaction as Success / Finalized. The wallet retained
0.499985 Devnet SOL after the three transaction fees. API-publication failures retain the receipt
and surface an error so a retry can reuse the confirmed transaction.

## Performance and reproducibility

The combined-source `make bench` run completed successfully. All 12 durable runs and all eight
recovery comparisons matched independent replay. The three raw result files share the source hash
above. See [PERFORMANCE.md](PERFORMANCE.md) for the generated report: risk p50/p99 1.8/3.1 us;
durable medians 110/345/482 orders/s at 1/10/50 clients; 105,000-event recovery 1.95 s full
versus 0.44 s from checkpoint plus 5,000-event suffix. These are measured runs, not a controlled
comparison with earlier reports. The production terminal remained running during the benchmark;
its normal polling and WebSocket connection were part of the background workload.

Redacted local logs and supplementary evidence are in ignored `runs/verification-20261010/`.
Credentials and wallet keys are excluded from the committed records. See [SUBMISSION.md](SUBMISSION.md)
for access and publication gates; passing these checks does not mean Devpost has been submitted.

## Judge demo and full-trace seal (October 10, 2026)

Operator authentication now protects HTTP and WebSocket access. Browser sessions use
expiring HttpOnly cookies; untrusted origins are rejected. The isolated auditor seals
16 durable record sets, including agent messages and strategy sales. See [SECURITY](SECURITY.md).
The earlier complete security suite passed 141 tests; two subsequent focused checks
passed, including a newly added sealer crash-recovery case. These are prior evidence,
not a claim that the latest interrupted full-suite run completed.

The production web build completed successfully in 79.41 seconds. The exhaustive
benchmark and additional full-suite rerun were stopped at the user's request to
prioritize a working judge presentation. Partial results remain local; the committed
performance tables are explicitly historical. The report generator refuses stale,
incomplete or mixed-source results.

The active `quantswarm_judges` database preserves the actual demo records while the
original `quantswarm` database retains all historical benchmark data. The copied
portfolios contain 2,177 (`main`) and 2,163 (`real-check-20261010`) ledger events.
Research, marketplace and market-data records were copied as well. This is a new
audit baseline, not retroactive authentication of the import's earlier history.

The running auditor published and independently verified a finalized Devnet seal:

- Anchor: `3e8f9cb473834b11bcc5bc5169b412d7`.
- Manifest SHA-256: `7f671102f015f90b7b6315758e4c70eb3d6978b41b808b7080997d7be2c752d2`.
- [Actual Solana transaction](https://explorer.solana.com/tx/4A6j35zFwq8PN9uKvKkQqyeBrH11mp3CMjj1xngkRWVWegx3CxeK6Yn6a3LzEdg941QztcrMdFtX5VJQRxM6zgY9?cluster=devnet).
- [Public receipt](../data/audits/3e8f9cb473834b11bcc5bc5169b412d7.json).
- All 16 record roots matched: 9,603 sealed records, including 106 agent messages and
  two strategy sales; zero pending changes at verification.

The browser's **Verify blockchain & records** action displayed the same successful
9,603-record result, alongside a matching 2,177-event main-portfolio replay and a
running audit service. The judge demo tab is left open on that result.

The production terminal is running at http://localhost:3000. Use [JUDGE_DEMO](JUDGE_DEMO.md)
for the presentation sequence. API, worker, manager and periodic auditor are running.
