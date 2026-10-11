# Simulation accounting and replay performance

The user reported that agent losses disagreed with the graph and replay was slow.

## Diagnosis

- At the captured 456-step checkpoint, sleeves summed exactly to portfolio P&L: -$485.13. The separate full-budget passive baseline was -$760.84. The passive agent card represented only its $2,250 sleeve, not the $9,000 deployed baseline.
- The screen combined streamed pre-fill account values, coordinator sleeve checkpoints, and a separately polled baseline. These could describe different trading instants.
- The full report was 9,913,421 bytes and took 13,189 ms to fetch under the recorded load. Full growing history was written every five cycles and polled by the UI.
- Serial per-symbol API calls and unnecessary final empty event fetches dominated market processing.
- Completed managed portfolios retained execution workers indefinitely, consuming most of the manager's 1 GiB memory allowance.

## Verification

- 32 focused native tests passed; 36 Linux tests passed, including managed research, approved and experimental simulations, and compact live endpoint coverage. See `managed-tests.xml`.
- Serial and batched fixtures produced identical financial ledger events, digests, and attribution. Resting orders retain serial processing.
- Browser run `sim-mv3ypauu`: all 100 financial checkpoints exactly match the saved pre-change replay, including sleeve positions, cash, costs and P&L. The ledger digest also matches. All four agents remain funded and all 19 actual execution fills are preserved.
- Recorded replay time before worker cleanup: 164.3 seconds before versus 87.7 seconds after (1.87x). Full job wall time: 209.4 versus 96.6 seconds. These are observed runs on a shared local Docker host, not an isolated hardware benchmark.
- Portfolio finish: $9,888.11 (-$111.89 / -1.1189%); full-budget passive baseline: $9,751.62 (-$248.38 / -2.4838%). Fee $0.20; slippage $4.84; accounting residual $0.
- Desktop and mobile browser checks passed, four agent cards rendered, the comparison table reconciled, full history exported, and ledger chain verification passed with no uncaught browser exceptions.

The 100-step comparison is a regression check, not a claim of full-horizon profitability or future returns.

## Final build checks

- Worker lifecycle regression passed: park, automatic wake, idempotency, manual restart, active-job protection, resting-order protection, and ledger verification. See `worker-lifecycle-tests.xml`.
- Final-source 100-step browser run `sim-mv3z465z` again matched all financial checkpoints and the original ledger digest. Replay time was 69.5 seconds versus 164.3 seconds before (2.36x); full job wall time 91.3 versus 209.4 seconds. See `reference-comparison.json`.
- The full-period stress run was stopped at the user's request to skip further testing and commit. Full-horizon performance is not claimed by this change.
