# Strategy evaluation against buy-and-hold

Does the research tournament do better than simply holding every symbol? This page records how that was
measured, what was found, and what the numbers do not show.

## What is compared

- **Baseline:** equal dollar slots in every symbol, 90% of capital, whole shares, bought once and never
  touched. It is the tournament's passive core run on its own, through the same engine, fees, slippage,
  participation cap and reservation rules as every strategy.
- **Portfolio:** what the tournament froze: funded strategies at their allocated capital, with the rest of
  the deployable 90% in the passive core. If nothing is funded the portfolio *is* the baseline.

All figures are for the held-out test split (the last 20% of each dataset, about nine months of hourly
bars), which is evaluated once after everything is frozen.

## Method

One held-out period says very little. Across otherwise identical synthetic markets the baseline's own
return ranges from about −24% to +53%, so a single comparison is dominated by luck. The evaluation therefore
uses many independent markets drawn from the fixture's generator with different seeds
(`scripts/evaluate_vs_baseline.py`, `make evaluate`).

- Design work used seeds 1–30 only (strategy family in a simplified simulator, then the full tournament on
  seeds 1–11).
- Seeds 101–130 were held back and run once with the frozen code. Nothing was tuned against them.
- Runs use the recorded proposals and the deterministic supervisor, so they are reproducible without an LLM.

## Results

Final evaluation, seeds 101–130 (`artifacts/strategy-eval/final-seeds-101-130.json`):

| | Portfolio | Baseline |
|---|---|---|
| Mean held-out return | +5.11% | +4.43% |
| Mean excess return | **+0.68%** (standard error 0.71%) | |
| Mean Sharpe | 0.35 | 0.26 |
| Mean max drawdown | 14.2% | 15.7% |

In 9 of the 30 markets no strategy earned capital and the portfolio equals the baseline. In the other 21 the
portfolio was ahead on return in 13 and had the smaller drawdown in all 21. The return edge is positive but
within one standard error of zero: on this evidence the portfolio is not worse than buy-and-hold and takes
less risk, and it is **not** shown to earn more.

For comparison, the previous scoring (absolute validation Sharpe, unearned capital left in cash) on the same
30 markets with its own recorded proposals returned −0.13% on average, an excess of **−4.58%** (standard
error 3.17%), ahead in 12 of 30 (`artifacts/strategy-eval/previous-pipeline-seeds-101-130.json`).

Per strategy, standalone with the full deployable capital, against the baseline on the same 30 markets:

| Strategy | Mean excess return | Ahead in | Mean Sharpe difference | Funded in |
|---|---|---|---|---|
| Regime-reversion overlay (mean_reversion agent) | **+4.16%** (s.e. 1.76%) | 17/30 | +0.36 | 21/30 |
| Moving-average crossover (momentum agent) | −5.98% (s.e. 2.29%) | 10/30 | −0.57 | 5/30 |
| Volatility target (volatility agent) | −6.93% (s.e. 2.68%) | 10/30 | −1.08 | 0/30 |

The overlay is the only strategy with an edge, and it is a real one in this family of markets. The portfolio
captures a fraction of it because the overlay averages 23% of capital: each strategy is capped at 40%, and
the evidence gate withheld funding in 9 markets. The overlay did about as well in the markets where it was
not funded (+3.8%) as where it was (+4.3%), so the gate did not pick its good markets; with three years of
history per market the out-of-sample evidence for an edge of this size is simply too noisy to tell.

The 40% cap is a risk policy, not a finding. `QS_CAP_PER_STRATEGY` changes it and is recorded in each
manifest's policy; a higher cap lets the overlay steer more capital and moves the portfolio toward the
standalone row above, with correspondingly larger swings when it is wrong. That variant has not been
evaluated as a portfolio.

Committed fixture (`synthetic-1h-seed42`) and real data, same frozen code
(`artifacts/strategy-eval/final-fixture-and-real.json`):

| Dataset | Portfolio | Baseline | Funded |
|---|---|---|---|
| Synthetic fixture, seed 42 | +2.69%, Sharpe 0.27, drawdown 13.8% | identical | nothing |
| Alpaca SIP 1-hour, SPY/QQQ/AAPL/MSFT/NVDA, test Jan–Sep 2026 | +15.78%, Sharpe 1.39, drawdown 10.1% | identical | nothing |

On both, no strategy beat the passive core out of sample, so the portfolio holds the core and matches the
baseline exactly. On the real data the overlay's out-of-sample excess was clearly negative (t = −2.6) and it
would have returned +8.6% against the baseline's +15.8% in the test period: the range-bound regimes it
relies on are a property of the synthetic generator, not of 2023–2026 US large caps.

## Why the overlay works on the fixture

The generator alternates between trending regimes (a random walk with a random drift) and mean-reverting
regimes (a pull toward an anchor level). The overlay stays invested by default. It estimates the trailing
mean-reversion speed of the equal-weight index of its universe; only while that speed is above what a random
walk produces does it sell once the index is stretched above its rolling mean, and it buys back when the
index returns. In trending regimes it does nothing. Trend-following families lose in the same markets
because every mean-reverting stretch makes them buy high and sell low.

## What changed in the tournament

- Strategies are scored on return in excess of the passive core, on bars they were not fitted on
  (two walk-forward training blocks plus validation), instead of on absolute validation Sharpe.
- Capital is scaled by the strength of that evidence, using the weaker of two t-statistics: excess return,
  and alpha after removing market exposure. A strategy that merely held less of a falling market gets nothing.
- Capital no strategy earns stays in the passive core instead of cash.
- A slot that is fully in cash is re-sized to its share of current equity before it re-enters, so realized
  gains are reinvested.
- The baseline is the passive core on its own. Before this, whole-share rounding left the baseline about 74%
  invested on real data, which made any fully invested portfolio look better than it was.

## What this does not show

- Nothing here is evidence about real markets. The one real dataset shows no edge for any strategy.
- The synthetic markets share one generator. An edge that exists by construction in that generator was
  found; that is all.
- Fills are a bar-based model (next open, fixed slippage, 1% of bar volume), not a replay of executions.
- With an LLM configured, proposals differ from the recorded ones, and so will the results.

## Reproduce

```
make evaluate SEEDS=101-130
uv run python scripts/evaluate_vs_baseline.py --dataset data/fixtures/synthetic_1h
```
