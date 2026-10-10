# P3 submission handoff

P3 work is on branch `p3-core`, in [PR #1](https://github.com/AryanSingh103/QuantSwarm/pull/1).
This is not a claim that P1/P2 or the overall HackRU submission are complete.

## Implemented and tested

- Event-maintained per-agent cost basis and buy reservations; full recomputation checks and working-order scaling benchmark.
- Atomic complete recovery checkpoints, including retry decisions; verified suffix replay and final digest comparison.
- Corruption, rollback, before/after-commit crash and repeated-termination coverage; independent full-history audit retained.
- `make prove`: 73 passed, zero failed, zero skipped, all 11 invariant categories passing on Windows with a real TimescaleDB container.
- Economic digest compatibility checked against the original ledger implementation on a buy/fill/sell event log.
- Updated architecture and limitations; Devpost copy in [DEVPOST.md](DEVPOST.md).

## Recorded performance

`make bench` completed all engine, durable and recovery workloads. All 12 durable runs and every recovery comparison passed verification; the three JSON artifacts share one source hash. At 105,000 events, full recovery measured 6.61 s versus 1.55 s from checkpoint + 5,000-event suffix. Durable medians were 57/231/181 orders per second at 1/10/50 clients. See [PERFORMANCE.md](PERFORMANCE.md) for hardware, percentiles and methodology.

## Submission gates still requiring external action

- **Judge access:** the repository is private. The authenticated collaborator has push access but no admin/maintain access. The repository owner must make it public or grant judges access.
- **Devpost:** the write-up is prepared in the repo, not submitted. A project/submission URL has not been supplied, and attempts to control the open Devpost tab timed out. A teammate with working Devpost access must publish/submit the final project and verify its status.
- **P1/P2:** their demo, terminal and keyed integration checks remain separate work. This P3 verification does not certify those milestones.

## Reproduction

Follow the README setup, then run `make prove` and `make bench`. For the large real-data benchmarks,
first run `uv run python scripts/ingest_binance.py`; downloads are checksum-verified and excluded from
timing. Without it, the committed synthetic fixture works offline and unavailable larger sizes are disclosed.
`docs/PERFORMANCE.md` is generated from `benchmarks/results/*.json`; source hashes must agree before a report
can be generated. Core changes after these measurements require another benchmark run.

Do not mark P3 or the overall submission complete until the access and Devpost gates are visibly verified.
