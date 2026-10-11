"""Exact integer accounting attribution and explicitly approximate exposure diagnostics."""
from __future__ import annotations

import numpy as np
from datetime import datetime
from hellabandz.trading.allocation import NY


class ReturnAttribution:
    def __init__(self):
        self.instruments = {}
        self.residual = 0

    def record(self, previous_qty, previous_marks, marks, fills, equity_change):
        cycle = 0
        for sym in sorted(set(previous_qty) | {f["symbol"] for f in fills}):
            row = self.instruments.setdefault(sym, {"mark_pnl": 0, "execution_pnl_at_close": 0,
                                                   "fees": 0, "modeled_slippage": 0, "traded_notional": 0})
            mark_pnl = previous_qty.get(sym, 0) * (marks.get(sym, 0) - previous_marks.get(sym, marks.get(sym, 0)))
            row["mark_pnl"] += mark_pnl
            cycle += mark_pnl
        for f in fills:
            row = self.instruments[f["symbol"]]
            impact = f["qty"] * (marks[f["symbol"]] - f["px"]) * (1 if f["side"] == "BUY" else -1)
            row["execution_pnl_at_close"] += impact
            row["fees"] += f["fee"]
            row["modeled_slippage"] += f.get("slippage", 0)
            row["traded_notional"] += f["qty"] * f["px"]
            cycle += impact - f["fee"]
        self.residual += equity_change - cycle

    def report(self, pnl, cash, initial_cash):
        rows = {s: {**r, "net_pnl": r["mark_pnl"] + r["execution_pnl_at_close"] - r["fees"]}
                for s, r in sorted(self.instruments.items())}
        net = sum(r["net_pnl"] for r in rows.values())
        return {"units": "integer USD micros", "instruments": rows, "initial_cash": initial_cash,
                "ending_cash": cash, "cash_interest_pnl": 0, "net_pnl": pnl,
                "gross_before_modeled_costs_pnl": pnl + sum(r["fees"] + r["modeled_slippage"] for r in rows.values()),
                "reconciliation_residual": pnl - net,
                "cycle_residual": self.residual,
                "methodology": "prior holdings times mark changes + fill quantity times signed close-minus-fill "
                               "price - fees; modeled slippage already embedded in fill prices, never subtracted twice"}


def relative_attribution(candidate, baseline):
    """Exposure-scaled comparator is a diagnostic, not executable alpha or causality."""
    if candidate.equity_ts != baseline.equity_ts or not candidate.telemetry or not baseline.telemetry:
        return {"available": False, "reason": "aligned telemetry required"}
    def returns(r):
        e = np.asarray([r.initial_cash] + r.equity, dtype=np.float64)
        return np.diff(e) / e[:-1]
    cr, br = returns(candidate), returns(baseline)
    ce = np.asarray([r["exposure_before_bps"] / 10_000 for r in candidate.telemetry])
    be = np.asarray([r["exposure_before_bps"] / 10_000 for r in baseline.telemetry])
    scale = np.divide(ce, be, out=np.zeros_like(ce), where=be > 0)
    matched = float(np.prod(1 + br * scale) - 1)
    cnet, bnet = candidate.final_equity / candidate.initial_cash - 1, baseline.final_equity / baseline.initial_cash - 1
    days = {}
    for ts, c, b in zip(candidate.equity_ts, cr, br):
        day = datetime.fromtimestamp(ts, NY).date().isoformat()
        row = days.setdefault(day, {"date": day, "arithmetic_candidate_return": 0., "arithmetic_baseline_return": 0.,
                                    "arithmetic_relative_gap": 0.})
        row["arithmetic_candidate_return"] += float(c)
        row["arithmetic_baseline_return"] += float(b)
        row["arithmetic_relative_gap"] += float(c - b)
    return {"available": True, "net_return_gap": cnet - bnet,
            "approximate_exposure_matched_baseline_return": matched,
            "approximate_exposure_component": matched - bnet,
            "approximate_selection_timing_and_cost_residual": cnet - matched,
            "mean_candidate_exposure_pct": float(ce.mean() * 100),
            "mean_baseline_exposure_pct": float(be.mean() * 100),
            "mean_candidate_cash_pct": float(100 - ce.mean() * 100),
            "largest_underperformance_days": sorted((r for r in days.values() if r["arithmetic_relative_gap"] < 0),
                                                      key=lambda r: (r["arithmetic_relative_gap"], r["date"]))[:5],
            "baseline_up_bars": int((br > 0).sum()),
            "baseline_up_bars_candidate_underexposed": int(((br > 0) & (ce < be)).sum()),
            "modeled_cost_difference_fraction": (candidate.fees + candidate.slippage) / candidate.initial_cash
                                              - (baseline.fees + baseline.slippage) / baseline.initial_cash,
            "methodology": "approximate per-bar baseline return scaled by prior candidate/baseline gross exposure; "
                           "zero where baseline exposure zero; composition, beta, execution and compounding differ; "
                           "residual does not identify causal timing skill or alpha. Daily gaps sum per-bar returns "
                           "within New York dates for diagnosis and are not compounded portfolio returns"}
