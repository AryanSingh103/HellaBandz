# Data sources

| Dataset | Status in repo | Use | Reproduce |
|---|---|---|---|
| `synthetic-1h-seed42` | **Committed** (`data/fixtures/synthetic_1h`, 34,195 bars, 5 symbols, 2023-01-03 → 2026-09-30) | Default for the tournament, live session, tests and demo; no keys needed | `make fixture` (seeded; content hash must match `manifest.json`) |
| Alpaca US equities, 1-hour bars | Script only (`scripts/ingest_alpaca.py`) | Real-data tournament: `--dataset data/snapshots/alpaca-…` | Free Alpaca account; `feed=sip` (history older than 15 min) or `feed=iex`; `adjustment=all`; regular hours only |
| Binance spot BTCUSDT 1-minute klines, Jan–Mar 2024 | Script only (`scripts/ingest_binance.py`), raw files gitignored | 131,040-bar replay benchmark (real market activity) | Public archive `data.binance.vision`; every ZIP verified against its published `.CHECKSUM` |
| Recorded agent proposals | Committed (`data/fixtures/proposals/*.json`) | Offline fallback when `OPENAI_API_KEY` is unset | Example LLM-style outputs, marked `source: fixture` in every manifest |

## Synthetic fixture

Regime-switching process: the market factor alternates between trending regimes (random drift) and
mean-reverting regimes (pull to an anchor); each symbol has its own beta and idiosyncratic noise. Calendar:
weekdays, 7 hourly regular-session bars at 14:30–20:30 UTC. Holidays and DST are **not** modeled. Prices
are rounded to cents. It exists so expected behavior is reproducible and judges need no accounts. It is
not a claim about real markets.

## Snapshot format and checks

`bars.parquet` (symbol, ts epoch-seconds, open/high/low/close int micro-units, volume int) +
`manifest.json` (source, symbols, interval, adjustment, row count, first/last ts, chronological 60/20/20
split boundaries, **content SHA-256 over the sorted columns**, so it is stable across Parquet writer
versions). `load_snapshot` refuses data whose hash does not match.

Validation (hard failures): empty data, unsorted rows, duplicate timestamps per symbol, non-positive prices,
`low > min(open, close)` or `high < max(open, close)`, negative volume. Intraday gaps are reported as
warnings and never filled, because filling would invent prices.

Binance specifics: spot files switched from millisecond to microsecond timestamps in 2025; the parser
detects the unit. Volume is converted to 0.001-base-unit lots because the engine trades whole units.

## Terms

Raw Alpaca and Binance data are not redistributed; scripts plus manifest hashes make them reproducible.
Kalshi was cut from scope (it needs its own binary-contract model and adds nothing to the core claim).
Databento was not used.
