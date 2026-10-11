# Simulation repair verification — October 11, 2026

Fixed the confusing post-merge simulation flow. The old coordinated mode funded only the passive core when challengers failed research gates, and agent cards looked for core orders under the aggregate execution owner, incorrectly reporting no trades. Preparation also appeared as an unexplained zero-step run.

Simulation now defaults to explicitly experimental paper funding: defensive, mean reversion, trend and passive core each receive $2,250; $1,000 remains unallocated. Approved-only mode remains available, registry promotions remain unchanged, and ordinary API callers default to approved allocations. Startup stages, recurring rule readings, five-cycle reports and virtual sleeve attribution make execution visible. The header Run research button was removed.

Validation:

- 34 Linux tests passed, including actual research, API, PostgreSQL, managed worker, approved replay and experimental replay. Experimental tests verify four equal budgets, unchanged registry, matched baseline timestamps and ledger integrity.
- TypeScript and production API/manager/web builds passed. All three services activated; auth remains disabled.
- Browser launched fresh experimental portfolio `sim-mv3xkcuo` on the original real SIP snapshot and completed 100 steps. History export, four funded agents, $9,000 total allocation, multiple agents with fills, matching baseline range and ledger verification passed.
- Final rendered UI checks confirmed four cards with attributed fills, both Experimental agents and Buy & hold everything lines, absent header button, and zero uncaught browser exceptions.

The fresh run had 19 aggregate paper CLOB fills, zero ledger rejections, zero reconciliation breaks, 400 rule readings and five recorded AI remarks. Virtual sleeve fill counts were defensive 2, mean reversion 3, trend 16, core 3; virtual attribution includes internal transfers and can differ from aggregate venue fills.

This 100-step smoke test ended at $9,888.11 (-1.1189%) versus $9,751.62 (-2.4838%) for the matched passive baseline. It is an experiment on previously inspected historical prices, not evidence of future returns. It is not the full-period benchmark reported previously.

The old full replay was stopped cleanly at 426 cycles before service activation, preserving its partial history. Duplicate background research requests were stopped. No real-money trading was enabled.
