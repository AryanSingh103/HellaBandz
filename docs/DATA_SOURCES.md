# Data sources

| Dataset | Status in repo | Use | Reproduce |
|---|---|---|---|
| `synthetic-1h-seed42` | **Committed** (`data/fixtures/synthetic_1h`, 34,195 bars, 5 symbols, 2023-01-03 → 2026-09-30) | Default for the tournament, live session, tests and demo; no keys needed | `make fixture` (seeded; content hash must match `manifest.json`) |
| Alpaca US equities, 1-hour bars | Script only (`scripts/ingest_alpaca.py`) | Real-data tournament: `--dataset data/snapshots/alpaca-…` | Free Alpaca account; `feed=sip` (history older than 15 min) or `feed=iex`; `adjustment=all`; regular hours only |
| Binance spot BTCUSDT 1-minute klines, Jan–Mar 2024 | Script only (`scripts/ingest_binance.py`), raw files gitignored | 131,040-bar replay benchmark (real market activity) | Public archive `data.binance.vision`; every ZIP verified against its published `.CHECKSUM` |
| Recorded agent proposals | Committed (`data/fixtures/proposals/*.json`) | Offline fallback when `OPENAI_API_KEY` is unset | Example LLM-style outputs, marked `source: fixture` in every manifest |

## Recorded real-data run (Alpaca)

`alpaca-sip-1hour-2023-01-03-2026-09-30`: SPY, QQQ, AAPL, MSFT, NVDA, 1-hour SIP bars, split/dividend
adjusted. 32,865 rows (939 trading days × 7 session bars × 5 symbols, no gaps), content SHA-256
`733b700631cb9167…` (full hash in the snapshot manifest). Splits: train 2023-01-03 → 2025-04-02,
validation → 2025-12-31, test → 2026-09-30.

Tournament `T20261010194733` (live GPT-4.1-mini agents and supervisor; manifest
`data/manifests/T20261010194733.json`):

| Agent | Family | Universe | Train SR | Val SR | Val DD | Stability | Score | Weight |
|---|---|---|---|---|---|---|---|---|
| momentum | sma_crossover | AAPL, QQQ, SPY | 1.41 | 1.12 | 5.9% | 1.00 | 1.053 | 37.1% |
| volatility | vol_target | AAPL, MSFT, NVDA | 1.65 | 1.39 | 7.0% | 0.67 | 0.863 | 30.1% |
| mean_reversion | zscore_reversion | AAPL, QQQ, SPY | 0.83 | 1.41 | 11.7% | 0.67 | 0.829 | 22.8% |

Held-out test (Jan–Sep 2026, evaluated once after the freeze): **+6.28%**, Sharpe 1.10, max drawdown 5.53%,
298 fills. That is one period and five large-cap symbols under the bar-fill model, so it is not evidence of
a durable edge (see LIMITATIONS).

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
