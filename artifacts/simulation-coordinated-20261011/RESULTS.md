# Coordinated simulation results — 2026-10-11

The full replay selected the passive core. It matched, rather than beat, the same-engine passive baseline. None of the three challenger roles earned funding after development/validation research.

| Metric | Selected portfolio | Matched passive baseline |
|---|---:|---:|
| Starting equity | $10,000 | $10,000 |
| Ending equity | $11,242.04 | $11,242.04 |
| Net return | 12.4204% | 12.4204% |
| Sharpe | 1.1942 | 1.1942 |
| Sortino | 1.7662 | 1.7662 |
| Maximum drawdown | 10.0467% | 10.0467% |
| Filled orders | 5 | 5 |
| Fees / slippage | $0.11 / $4.01 | $0.11 / $4.01 |

Dataset: `alpaca-sip-1hour-2023-01-03-2026-09-30`, SHA256 `733b700631cb91676aedd3c014fe4ddd7bccf193e0e904f7818256eceaa7c5e1`. The evaluation replay completed 1,315 hourly cycles. Prices are historical SIP bars; paper CLOB depth is synthesized from OHLCV, not observed exchange depth. $9,000 was allocated to the core and $1,000 remained unallocated.

The frozen financial implementation hash was `e49e77cf71a6afec75609164c8b96941c7605837f42dfff78aa1d3c6f93daad1`, policy `adaptive-allocation-3`, runtime `runtime-regime-units-4`. Later API-only fixes expose direct coordinated messages/context and do not change financial execution. Both financial ledgers had digest `a9d96894314b5f72bf2fb1f2d40f6981bd0bb3bf3024b6127b80fe8ce9f1ab1f`.

The full benchmark used deterministic local ledger transport. It completed research plus replay in 524.229 seconds; replay took 161.9 seconds. Peak RSS was 487.9 MiB under a 2 CPU / 1 GiB container limit on a shared host. There were zero ledger rejections, zero reconciliation breaks and zero attribution residual. These are historical results on an inspected evaluation set, not evidence of future profitability or an untouched test set.

The old downloaded simulation used the legacy independent-agent session and a different, synthetic dataset. Its -3.09% result cannot be directly compared with this +12.42% result as a strategy improvement.

The app now launches coordinated simulation directly, records frozen research provenance and funding decisions, preserves all rule readings, attributes portfolio results to virtual strategy sleeves, and exports a baseline over the same completed bars. Legacy historical sessions retain their legacy labels.

Validation: managed research → legacy session → direct coordinated session integration passed in 38.32 seconds using isolated PostgreSQL, actual API, manager and execution workers. Browser launch/export evidence and focused unit results accompany this report locally.

Focused tests: 98 passed. The browser created `sim-mv3lx4bs` through the actual launch controls, completed 100 real SIP steps and downloaded its history without uncaught browser errors. Its separate shorter-horizon result was $9,751.62 ending equity, -2.4838% return, -3.0541 Sharpe and 4.3085% maximum drawdown, exactly matching that horizon's baseline. Its 517-event ledger verified. Export checks confirmed 400 rule readings, four agent roles, 100 matched baseline timestamps, correct dataset hash and zero capital/P&L for unfunded shadows.
