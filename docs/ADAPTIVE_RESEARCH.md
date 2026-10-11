# Adaptive stock and ETF research

The loop searches for evidence of improvement; it cannot promise higher returns. The order book matches
orders and reconciles money. Strategy proposals and selection are a separate research responsibility.
All execution remains paper trading, with synthetic liquidity derived from OHLCV bars.

## Cross-sectional allocation and cadence

Policy `adaptive-allocation-2` and runtime `runtime-allocation-3` add two reviewed families:
`relative_strength` ranks trailing session returns; `risk_adjusted_rotation` divides those returns
by trailing annualized session volatility. Ties resolve alphabetically. Their eligible universe is
the available sector ETFs when at least three are registered: XLB, XLC, XLE, XLF, XLI, XLK, XLP,
XLRE, XLU, XLV and XLY. Otherwise the explicitly recorded fallback is the registered stock/ETF
universe. SPY and QQQ are excluded from sector rotation when sufficient sector ETFs are present.

Bounds: lookback 20–252 observed sessions, selected count 3–6, momentum threshold −10% to +20%,
and volatility window 10–126 sessions. Integer cadence `1` means daily; `5` means **calendar weekly**
at the first observed New York session of the ISO week, including holiday-shortened weeks.
House seeds test relative strength at 63/126 sessions, weekly, top four, threshold zero; risk-adjusted
rotation at 63 sessions, 21-session volatility, top four, threshold zero, daily and weekly.
These seeds share the existing eight-candidate budget per role with prior reviewed families.

Daily observations use the last available 09:00–15:59 New York hourly bucket close. The vendor's
09:00 bucket contains the 09:30 open and can contain premarket prices; this is a close proxy, not
exchange-certified session data. DST uses `America/New_York`; weekends are excluded. Holidays,
missing bars and partial sessions follow the actual observations. At a decision, ranking uses only
completed dates strictly before the current session and dates shared by every eligible member.
All members need a fresh current snapshot before a new decision freezes. Warm-up replays prior
calendar decisions, preserving a weekly decision across a midweek start. Daily state is bounded.

New intents explicitly use `weight_scope="sleeve"`: weights divide the **whole sleeve**, sum to at
most 100%, and unused selections remain cash. The coordinator validates the entire vector before
netting or orders. A new allocation identifier freezes integer quantities using fixed opening sleeve
capital and decision-time prices. Quantities remain unchanged between decisions; reduced executions
retry toward the same target without hourly value trimming. Each scheduled decision may resize using
the same fixed opening capital; gains are not automatically reinvested. Existing `slot` strategies
retain their original sizing. All account risk caps, fees, whole shares and internal netting remain.
Alphabetical processing can defer rotation buys until sells release cash on subsequent cycles.
The existing tech group cap is unchanged and does not include XLK; the instrument cap still applies.

Identical strategy version hashes across roles receive capital only once. Reports record distinct
development configurations, frozen versions and duplicate-funding rejection reasons. Legacy tournament
research intentionally excludes allocation families; its slot-based backtester refuses them explicitly.

## Attribution and future observation

Evaluation records exact instrument P&L in integer USD micros: prior shares × mark changes, plus
signed fill shares × (current close − fill price), minus fees. Instrument totals reconcile to account
P&L with an explicit residual. Slippage is already embedded in fills and is never subtracted twice.
The gross-before-modeled-costs diagnostic adds reported fees and modeled slippage back to net P&L;
it is not a frictionless execution replay. Cash earns zero interest in this model.

Optional telemetry includes timestamps, cash, equity, positions, marks and cost deltas. The exposure
diagnostic scales each baseline return by prior portfolio/baseline gross exposure, with zero where
baseline exposure is zero. It reports exposure contribution and a selection/timing/cost residual,
both explicitly approximate; these do not identify causal alpha. Largest underperformance days sum
per-bar return gaps within New York dates and are arithmetic diagnostics, not compounded returns.
Attribution is report-only and cannot enter validation/test optimization feedback.

To evaluate a registry on subsequent data **without refitting**, preserve the registry and run:

```powershell
uv run python -m hellabandz.trading.evaluate --dataset data/snapshots/FUTURE_SNAPSHOT --registry artifacts/adaptive/registry.json --frozen-registry --json artifacts/adaptive/forward.json
```

Optional `--start` and `--end` are inclusive/exclusive epoch seconds. The evaluator requires completed
current policy/code, unchanged frozen version hashes, matching registered universe, source/feed, price
adjustments, accounting units and hourly convention,
and a start after the original validation period. It performs no research, mutation or promotion; only
the explicit evaluation counter/history changes. Archive a copy of the registry before each observation.
Capture future bars with the same adjustment convention, keep decisions frozen, and accumulate a
predeclared observation window before comparing results. A newly downloaded directory with dates
already inspected is reused evidence: manifest `historical_periods_previously_inspected=true` forces
`holdout_untouched=false`, independently of its content hash and actual same-dataset view counter.
Historical snapshots, including expanded universes, cannot establish live profitability.

## Run it

From the repository root, with no model key required:

```powershell
uv run python -m hellabandz.trading.research --dataset data/snapshots/alpaca-sip-1hour-2023-01-03-2026-09-30 --registry artifacts/adaptive/registry.json --budget-s 600 --json artifacts/adaptive/research.json
```

Add `--llm-proposals` to ask the configured model for additive schema-validated ideas. Calls receive
development-only summaries, have no SDK retries, and share a ten-second budget per role, including one
schema repair attempt. Failure falls back to deterministic researchers. No arbitrary generated Python
is evaluated. The default CLI and historical evaluator are offline and deterministic.
Every proposed numeric value must be inside the reviewed bounds. Cartesian grid combinations that
violate relations such as `fast < slow` are discarded, with valid/discarded counts recorded; a grid
with no valid combination is rejected. Bounded schema-repair errors are included in proposal provenance.

For a frozen research policy followed by test reporting:

```powershell
uv run python -m hellabandz.trading.evaluate --dataset data/snapshots/alpaca-sip-1hour-2023-01-03-2026-09-30 --registry artifacts/adaptive/registry.json --budget-s 600 --json artifacts/adaptive/evaluation.json
```

The evaluator by default reruns research before test reporting. Each call counts validation reuse and
held-out views; rerunning a previously inspected dataset never makes it fresh evidence. To measure a
genuinely unseen test, register a new future snapshot and use the frozen-policy mode described below.

In the app, **Launch research** refreshes this loop's coordinated registry before the existing tournament
and analytics workflow. Its job result includes `adaptive_research`; its stages start with `adaptive:`.
The legacy tournament/manifest remains a separate comparison and keeps its existing contracts.
Its fit worker count defaults to one in managed research jobs (an explicit `QS_TOURNAMENT_PROCS`
operator setting is respected). The 600-second budget applies to the adaptive loop; subsequent
legacy manifest and analytics generation adds its own work to the research job.
Coordinated sessions refresh stale policy/code/dataset results and reuse completed current results.
An active session uses frozen versions; it does not hot-swap or optimize on losses. Unsupported legacy
datasets skip the adaptive loop and retain their previous tournament behavior.

## Agent responsibilities and chronology

1. **Proposal researchers:** trend explores SMA, breakout, time-series momentum and relative-strength rotation; mean reversion
   explores z-score, RSI and basket-regime overlays; defensive explores volatility sizing and regime
   overlays plus volatility-adjusted rotation. Each house hypothesis explains entry, exit and failure conditions in its strategy card.
   Optional model proposals choose only these reviewed families and bounded numeric parameters.
2. **Development optimizer:** sees only the first third of training. It ranks realized net excess,
   Sharpe improvement, drawdown and excessive turnover, plus a modest consistency term from two chronological
   halves of that same development equity path. Formula: return excess + .02 × Sharpe excess − positive
   drawdown excess − .001 × turnover above 60/year + .25 × minimum return excess across the two halves.
   These diagnostics never use later training, validation or test. Default three rounds, at most eight evaluated
   candidates per role across all rounds, 24 total. House family seeds precede additive model ideas.
3. **Development critic:** identifies high churn, deeper drawdown or weak excess and proposes adjacent
   parameter experiments. Each child records its parent, feedback, hypothesis, parameters, source,
   score and realized metrics. It never consumes validation or test feedback.
4. **Freeze:** choose one challenger per role using development only. Remaining training thirds
   become chronological forward checks of these already frozen challengers. Validation is one fixed
   paper-CLOB assessment per challenger, with the same initial capital and venue as the baseline.
5. **Promotion critic:** applies explicit gates, then tests the combined frozen allocation once on
   validation. Individual success cannot bypass failure of the combined allocation. Failed research,
   cancellation or deadline exhaustion leaves the passive basket; no partial portfolio is promoted.

Test bars are physically excluded from the research data provider. Proposal prompts are restricted
further to the development prefix. The final evaluator alone accesses the test, after selection.

## Gates and interpretation

The selection baseline is **equal-weight buy-and-hold of the registered universe**, through the same
paper venue and coordinator constraints. It is not implicitly SPY. When SPY exists in the dataset, the
test report includes `baseline_spy`, a holding subject to the coordinator's concentration/cash limits,
plus `baseline_spy_price_index`, a fully invested first-to-last test-close price reference. The constrained
holding can have substantially less than 100% SPY exposure. The price reference uses the dataset's price
adjustments and models no execution costs, cash reserve or whole-share rounding. SPY does not secretly
change the optimization objective. Simulated portfolios use $10,000 initial equity with $9,000 deployable capital;
integer shares and risk constraints may leave additional cash.

All validation gates are required for both an individual candidate and its combined portfolio:

| Metric | Gate |
|---|---|
| Net return after modeled costs | More than 0.10 percentage points above basket |
| Annualized Sharpe, zero risk-free rate | More than 0.05 above basket |
| Annualized Sortino, downside deviation relative to zero | Above basket |
| Maximum drawdown | No deeper than basket |
| Turnover, buy plus sell notional / initial equity | At most 60x per year |
| Modeled fees plus slippage / initial equity | At most 2% per year |
| Terminal return with one additional copy of costs deducted | Still above similarly stressed basket |
| Integrity | No reconciliation breaks or ledger rejections; at least 30 cycles and one fill |

Individual challengers also require positive aggregate excess on the two frozen forward training
blocks, and excess/alpha t-statistics above `max(1.96, sqrt(2*log(development_trials)))` across forward
blocks plus validation. This is a conservative search-aware heuristic, **not** a multiple-testing
confidence guarantee; serial dependence and repeated research remain limitations. The extra-cost
check is a terminal sensitivity calculation, not a complete alternate execution replay.

Sharpe and Sortino use 252 × 7 hourly bars/year. Only hourly stock/ETF datasets are accepted. Turnover
and fees are bounded; it would be misleading to require an active strategy to trade less than a basket
that buys once. Funding is at most 30% of deployable capital per passing role. Remaining capital stays
in the passive core. No leverage or shorting is introduced.

## Resource limits and provenance

The optimizer is serial, one worker, low priority (`nice 10` on Linux), and checks deadline/cancellation
inside simulations. Defaults: 600-second CLI budget, three rounds, eight candidates per role. Hard
configuration limits: five rounds and sixteen candidates per role. Native indicator initialization,
file I/O and a single model request are bounded operations but not preempted by the Python deadline.
On a two-CPU VM, leave one CPU for execution and run research separately, for example with Docker
`--cpus=1 --memory=1g`; do not assume low priority alone prevents tail-latency contention.

The registry records policy and code versions, dataset hash, splits, exact experiments, frozen
challengers, criteria, validation view count, portfolio gates and research history. Atomic writes and
an OS file lock prevent two research writers from publishing conflicting results to one registry.
A concurrent attempt fails promptly and can be retried after the active job finishes. Locks release
on process death. A rejected version stays rejected. Stale/partial research is never considered
current. An interruption requires a new run; it does not resume incomplete economic execution.

Many hypotheses share a small five-symbol universe. Parameter optimization and one validation period
cannot establish future profitability. Use additional data, future paper observation and independent
market periods before trusting a winning backtest. The earlier fixed-grid benchmark numbers in
COORDINATED_TRADING.md remain labeled as results of the previous policy.
## Focused defensive challenger research

For the basket regime challenger, use a separate registry:

```powershell
uv run python -m hellabandz.trading.research --dataset data/snapshots/<dataset> --registry data/registry/defensive-challenger.json --profile defensive_challenger --budget-s 1200
uv run python -m hellabandz.trading.evaluate --dataset data/snapshots/<dataset> --registry data/registry/defensive-challenger.json --frozen-registry
```

This explicit profile tests 32 paired configurations of the original regime family
and the reviewed `regime_fixed_units` treatment: lookback 100/200, reversion threshold .025/.075, sell z-score
1.5/2.5, rebuy z-score 0/.5; regime lookback remains 200. It overrides rounds and
candidate count to one complete fixed grid and rejects model proposals. Every
point reaches the existing first-third training score before one challenger is
frozen. The remaining training blocks, validation, combined-portfolio checks,
baseline, costs and promotion criteria remain unchanged. This prevents
the smaller adaptive mutation budget from spending all its capacity on early
alphabetical parameter keys before testing the sell threshold.

Default app research still uses the broad three-role profile. A focused registry
does not satisfy the app's broad research cache. Reusing previously viewed
historical periods is exploratory evidence; parameter search does not establish
profitability or an independent market edge. The original hold-sizing rule rebases
entry cost to fixed opening sleeve capital after an exit, which can permanently
reduce share counts relative to passive holding during appreciation. The new family
preserves its opening integer target after exits. The target is capped to the first
risk-approved buy/internal transfer, survives partial fills, and cannot grow with
subsequent prices/equity. Every reacquisition remains subject to current cash,
concentration and portfolio limits; it may fail to reacquire all shares after an
adverse exit. Historical warmup creates no target. Anchors live in the coordinated
session state; stopping and restarting creates a new session/anchor, as with the
rest of the paper runtime. The legacy slot backtester/tournament reject this family
instead of silently applying different sizing. No live execution is enabled.
