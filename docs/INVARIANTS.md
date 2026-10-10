# Correctness invariants

**Headline claim.** No sequence of duplicated, concurrent, malformed, delayed, or crash-interrupted agent
commands can create cash or shares out of nothing, overdraw shared cash, short a stock, or apply a fill twice,
and the ledger can always be rebuilt bit-for-bit from its hash-chained event log.

Run `make prove` to execute every test and print this table with live pass/fail counts.

| ID | Invariant | How it is enforced | Evidence (tests) |
|---|---|---|---|
| I-01 | **Idempotency.** Retrying a command never has a second economic effect; reusing an id with a different payload is a conflict, never an overwrite | `commands.command_id` UNIQUE + `ON CONFLICT DO NOTHING` at the API; the engine also remembers every decided `client_order_id` | `test_duplicate_order_has_no_second_effect` (100 sequential retries), `test_100_concurrent_retries_of_one_order_have_one_effect` (100 concurrent HTTP retries, real Postgres), `test_same_id_different_payload_is_conflict_not_overwrite`, property test |
| I-02 | **Cash safety.** `0 ≤ reserved_cash ≤ cash` always; fills never cost more than the order's reservation | Buys reserve cash at approval (last close +2% + fee); fills are capped by remaining reservation, so a gap-up gives a partial fill, never debt | `test_concurrent_oversized_orders_cannot_overdraw_shared_cash` (50 concurrent oversized orders + 40% gap), `test_gap_up_partial_fill_never_exceeds_reservation`, property test with random ±30% gaps |
| I-03 | **Position safety.** Long-only: quantities never negative; sells limited to the agent's own unreserved shares | R5 + per-agent sub-accounts (`agent|symbol`) + sell reservations | `test_r5_cannot_sell_more_than_held_or_short`, `test_sell_is_per_agent_subaccount`, property test |
| I-04 | **Risk enforcement.** Every accepted order passed R0–R9; no LLM or UI path bypasses the engine | The worker is the only writer; API only inserts commands; the supervisor LLM is clamped by the allocator | `test_r1…`–`test_r9…`, `test_malformed_and_bypass_attempts`, `test_supervisor_cannot_move_weight_beyond_band_or_fund_failed_strategy` |
| I-05 | **Execution validity.** No fill on the decision bar; MKT at next open ± slippage; LMT only when traded through; ≤ 1% of bar volume; expiry releases reservations | `fill_for_bar` | `test_no_fill_on_the_decision_bar…`, `test_limit_buy_only_fills_when_traded_through`, `test_volume_cap_partial_fill_then_expiry_releases_cash`, `test_stress_bar_uses_stress_slippage` |
| I-06 | **Determinism.** Same data + config + commands ⇒ identical events, trades, equity and digests | Pure engine, integer money, fixed iteration orders, no clocks or randomness in the core | `test_identical_inputs_identical_outputs_and_replay`, `test_processing_is_deterministic` (property) |
| I-07 | **Accounting identity.** `cash + Σ cost_basis = initial_cash + realized_pnl − fees` exactly, after every command | `check_invariants` runs after every batch in the worker (a violation aborts the transaction) and after every step in tests | `test_realized_pnl_and_accounting_identity`, property test (checked after every command) |
| I-08 | **Recovery.** A crash at any point loses no committed event and creates no duplicate fill; a corrupted projection or log is detected | Events + command status + projections commit in one transaction; startup replays and verifies the hash chain and digest; advisory lock = one writer | `test_crash_mid_stream_then_restart[before_commit/after_commit]`, `test_repeated_sigkill_under_load` (random SIGKILLs), `test_worker_refuses_to_start_on_tampered_projection`, `test_tampered_event_log_detected`, `test_second_worker_blocks_on_single_writer_lock`, `test_worker_down_commands_queue_then_process_in_order` |
| I-09 | **Causality.** No strategy or backtest observes data after its decision time | Strategies are causal functions (truncation-invariant); backtests only load history `< end_ts`; R7 rejects future/stale signals; fills only on later bars | `test_signals_are_causal_under_truncation` (4 families × 25 random cuts), `test_signals_ignore_future_perturbation`, `test_backtest_does_not_read_data_after_split_end` (poisoned future data ⇒ identical result) |
| I-10 | **Audit integrity.** Altering a dataset, event, or tournament manifest is detected | Content hash on snapshots, SHA-256 event chain, manifest hash committed to Solana | `test_tampered_snapshot_refused`, `test_event_chain_detects_tampering`, `test_tampered_manifest_fails_verification` |
| I-11 | **Ordering.** Equal-timestamp events follow a documented order; input row order is irrelevant; out-of-order/duplicate bars are rejected without effect | `(ts, symbol)` sort; per-symbol monotonic `last_bar` check | `test_input_row_order_does_not_change_results`, `test_out_of_order_and_duplicate_bars_rejected_without_effect` |

The property test (`tests/property/test_ledger_properties.py`) generates up to 150 random commands per case:
buys/sells/limit orders from four agents (one unallocated), quantities including ≤ 0, exact retries,
same-id-different-payload retries, cancels by the wrong agent, kill-switch toggles, and bars with ±30%
gaps, ±35% moves, zero volume and stress flags. It checks the invariants after **every** command. As a
sanity check it was run against two deliberately broken engines (fills not capped by reservations, wrong
cost-basis removal); both were caught.

## Risk rules

| Code | Rule (defaults in `EngineConfig`) | Effect |
|---|---|---|
| R0 | Portfolio must be open | reject |
| R1 | Order value ≤ 25% of equity (at reservation price) | reduce to fit |
| R2 | Agent cost exposure + open buys ≤ agent allocation; unallocated agents cannot buy | reduce / reject |
| R3 | Market value + open buys ≤ 90% of equity | reduce |
| R4 | Cost + fees ≤ unreserved cash | reduce |
| R5 | Sell ≤ agent's unreserved position (no shorting) | reduce / reject |
| R6 | qty > 0 integer; known symbol; limit > 0 and within ±10% of last close | reject |
| R7 | `signal_bar_ts` not in the future and ≤ 2 hours older than the latest bar | reject |
| R8 | Same `client_order_id`: identical payload ⇒ original decision; different payload ⇒ conflict | no new effect |
| R9 | Kill switch (manual, or automatic at 15% drawdown from peak): blocks buys, cancels working buys; sells still allowed | reject |

REDUCE always reports only the tightest rule(s) that bound the quantity.

## Money and rounding

- Cash and prices: integer micro-dollars. Quantities: whole shares (Binance replay uses 0.001-unit lots).
- Market buy fill price = `ceil(open × (10000 + slip_bps) / 10000)`; market sell = `floor(open × (10000 − slip_bps) / 10000)`. Rounding always goes against the trader.
- Sell cost-basis removal = `floor(basis × q / position_qty)`; the remainder stays in the position, so the accounting identity holds exactly in integers.
- Fees: $0.005/share (models spread/commission; Alpaca equity commission itself is $0).
