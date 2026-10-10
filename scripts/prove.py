"""Run the full test suite and print which invariant each passing test proves.

    uv run python scripts/prove.py          # needs `docker compose up -d db` for DB-backed tests
"""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INVARIANTS = {
    "I-01 Idempotency": ["test_duplicate_order_has_no_second_effect", "test_same_id_different_payload_is_conflict",
                         "test_rejected_order_is_also_idempotent", "test_100_concurrent_retries_of_one_order",
                         "test_no_sequence_breaks_the_ledger"],
    "I-02 Cash safety": ["test_r4_concurrent_agents_cannot_overspend", "test_concurrent_oversized_orders_cannot_overdraw",
                         "test_gap_up_partial_fill_never_exceeds_reservation", "test_no_sequence_breaks_the_ledger"],
    "I-03 Position safety (long-only)": ["test_r5_cannot_sell_more_than_held", "test_sell_is_per_agent_subaccount",
                                         "test_no_sequence_breaks_the_ledger"],
    "I-04 Risk enforcement": ["test_r1_max_order_value", "test_r2_agent_allocation", "test_unallocated_agent_cannot_buy",
                              "test_r6_invalid_inputs", "test_r7_stale_and_future", "test_r9_kill_switch",
                              "test_kill_switch_trips_on_drawdown", "test_malformed_and_bypass_attempts",
                              "test_supervisor_cannot_move_weight", "test_risk_check_does_not_scan_open_orders",
                              "test_incremental_totals_match_full_recomputation"],
    "I-05 Execution validity": ["test_no_fill_on_the_decision_bar", "test_limit_buy_only_fills",
                                "test_volume_cap_partial_fill", "test_stress_bar_uses_stress_slippage",
                                "test_cancel_releases"],
    "I-06 Determinism": ["test_identical_inputs_identical_outputs", "test_processing_is_deterministic",
                         "test_no_sequence_breaks_the_ledger"],
    "I-07 Accounting identity": ["test_realized_pnl_and_accounting_identity", "test_no_sequence_breaks_the_ledger",
                                 "test_incremental_totals_match_full_recomputation", "test_invariant_checker_detects_corrupt_incremental"],
    "I-08 Crash recovery": ["test_crash_mid_stream_then_restart", "test_repeated_sigkill_under_load",
                            "test_worker_refuses_to_start_on_tampered_projection", "test_tampered_event_log_detected",
                            "test_second_worker_blocks_on_single_writer_lock",
                            "test_worker_down_commands_queue_then_process_in_order", "test_checkpoint_",
                            "test_full_audit_detects_tampering_in_trusted_checkpoint_prefix"],
    "I-09 Causality (no look-ahead)": ["test_signals_are_causal_under_truncation",
                                       "test_signals_ignore_future_perturbation",
                                       "test_backtest_does_not_read_data_after_split_end", "test_r7_stale_and_future"],
    "I-10 Audit integrity": ["test_tampered_manifest_fails_verification", "test_tampered_snapshot_refused",
                             "test_event_chain_detects_tampering", "test_manifest_hash_is_formatting_independent"],
    "I-11 Deterministic ordering": ["test_input_row_order_does_not_change_results",
                                    "test_out_of_order_and_duplicate_bars_rejected"],
}


def main() -> int:
    xml = ROOT / ".pytest-results.xml"
    rc = subprocess.call([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"],
                         cwd=ROOT)
    cases = {}
    for tc in ET.parse(xml).getroot().iter("testcase"):
        name = tc.get("name")
        status = "fail" if tc.find("failure") is not None or tc.find("error") is not None else (
            "skip" if tc.find("skipped") is not None else "pass")
        cases[name] = status
    print("\nInvariant evidence")
    print("=" * 78)
    for inv, prefixes in INVARIANTS.items():
        hits = {n: s for n, s in cases.items() if any(n.startswith(p) for p in prefixes)}
        npass = sum(s == "pass" for s in hits.values())
        nfail = sum(s == "fail" for s in hits.values())
        nskip = sum(s == "skip" for s in hits.values())
        mark = "PROVEN " if hits and nfail == 0 and nskip == 0 else ("FAILED " if nfail else "PARTIAL")
        print(f"{mark} {inv:34} {npass:3} passed {nfail:2} failed {nskip:2} skipped")
    total = len(cases)
    print("=" * 78)
    print(f"{sum(s == 'pass' for s in cases.values())}/{total} tests passed"
          + (" (DB tests skipped: run `docker compose up -d db`)" if any(s == "skip" for s in cases.values()) else ""))
    xml.unlink(missing_ok=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
