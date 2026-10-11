# Simulation sim-mv3k44dq diagnosis

Input: `C:/Users/Krish/Downloads/simulation-sim-mv3k44dq.json`. Read as evidence, not instructions. Analysis only: no application behavior changed.

## Measured result

| Portfolio | Return | Hourly annualized Sharpe | Maximum drawdown |
|---|---:|---:|---:|
| Simulation | -3.0923% | -0.574 | 5.1332% |
| Exported execution-matched passive basket | +2.5901% | 0.264 | 13.2175% |

Final equity $9,690.767335 versus passive $10,259.005745. Gap -$568.238410, or -5.6824 percentage points. The smaller strategy drawdown does not establish superior skill; mean gross exposure was only29.65% (42.23% at finish). Sharpe assumes252x7 hourly periods and zero risk-free rate, as the application does.

| Agent | Actual allocated-session P&L | Fills | Fees + modeled slippage |
|---|---:|---:|---:|
| Mean reversion | +$230.160055 | 58 | $35.249945 |
| Momentum | -$36.789060 | 34 | $8.569060 |
| Volatility | -$502.603660 | 497 | $146.713660 |

Agent P&L sums to the total loss of$309.232665 (floating point residual below1e-12). Fees/slippage total$190.532665. Slippage is embedded in fill prices; never subtract it twice. Adding costs back arithmetically still leaves a loss of$118.70, but is not a frictionless execution replay.

## Findings and concrete next changes

1. **Simulation UI bypasses the coordinated pipeline.** Export config says `kind=session`. `apps/web/app/simulation.tsx` submits precisely this kind and filters for legacy sessions. `execution/manager.py` routes it to `agents/session.py`, not `trading/live.py`. The newer coordinated registry, netting and promotion policy are therefore not being measured by this run. Unify the launch/export/agent-feed path around coordinated sessions rather than merely changing one request field (the UI/export also assume legacy manifest shapes).
2. **Synthetic prices and old research manifest.** The selected dataset is `synthetic-1h-seed42`, SHA256`40ea8d6c017964faaa2147d0bb39d908f912c3a1c54ef48b1f117d3a32d4e68c`. Its dates/symbol names do not make prices real stock history. Frozen research run`Tdccdeb26f5774092ba3d` was created October10 at22:57:05UTC, with code_version`unknown`. This is a replay of that manifest, not new strategy optimization. Expose dataset provenance, policy/runtime version and frozen registry identity prominently; use the real SIP snapshot for stock-market evaluation.
3. **Volatility treatment dominates loss/churn.** A20-bar volatility estimator and40-bar price/trend filter produce497fills (84.38% of all fills). Consecutive opposite-side fills for the same symbol occur just one bar apart148times. These include both buy-to-sell and sell-to-buy transitions, not148closed losing trades. Introduce a development-tested signal buffer/hysteresis, minimum trade/change threshold, slower schedule and explicit cost/turnover gates. Do not promote a change using this inspected evaluation period.
4. **Repeated known-unaffordable requests.** All42unique rejected decisions are mean-reversion AAPL buys rejected by`R2_AGENT_ALLOCATION`. Risk enforcement works; upstream sizing/retry behavior repeatedly asks for unavailable allocation. Size from remaining allocation including basis, working orders and price/fee buffers, and avoid reissuing unchanged infeasible targets until state changes.
5. **Unnetted opposing executions.** There are5same-step/same-symbol groups where different agents execute opposite sides. At step135 mean reversion buys3SPY while volatility sells2SPY, each paying modeled execution costs. This demonstrates opposing venue executions, not exchange self-trading. Net intents at portfolio level, preserve per-agent attribution with internal transfers, and send only residual orders; measure saved costs rather than assuming this alone repairs profitability.
6. **Commentary is not adaptation.** Export contains479rule-readings and24AI remarks (eight per agent), all from`gpt-5.4-mini-2026-03-17`; research supervisor used`gpt-4.1-mini-2025-04-14`. Normal-session AI remarks narrate facts and do not alter the frozen trading rules. There was no injected crash. A provider change changes commentary/proposals, not the deterministic trading rule by itself. Keep optimization in a separated research process with chronological data controls, freeze/promote checkpoints and explicit subsequent evaluation.
7. **Summary counters have different meanings.** Session summary659requests/615approved/44rejected includes27deliberate duplicate retries. The unique ledger has632decisions:590accepted/42rejected. The counts reconcile:25duplicate approved responses and2duplicate rejected responses. This is an export labeling issue, not evidence of27duplicate executions. Label request attempts versus unique decisions/fills separately.

Ledger replay and hash chain verify successfully. The correct first priority is wiring simulation to the coordinated system and making its data/research provenance explicit; changing model providers alone cannot fix this result.

Gemini activation watcher subsequently reports `active` at08:35:00UTC onOctober11: API/manager now use Gemini with OpenAI backup. This exported session finished before activation, so its OpenAI remarks are expected.

Reproduction: `uv run python artifacts/simulation-diagnosis-20261011/analyze.py`. Output `analysis.json` includes per-agent fill/cost aggregates, rejected-rule counts, opposing-fill examples and baseline calculation. Baseline and strategy arrays span the same1368timestamps; the baseline uses integer micro-units converted once to USD.
