"""Coordinated research: choose strategy versions on past data, fund only what beats the passive core.

Chronology, per dataset (splits from the registered snapshot: train 60%, validation 20%, test 20%):

1. Fit (train). Every candidate in a role's bounded grid runs alone, with the whole deployable
   capital, through the coordinated runtime in historical_sim on three consecutive training blocks,
   next to the passive core on the same blocks. Walk-forward: the candidate picked from blocks before
   block k is judged on block k (k = 2, 3), which gives out-of-sample evidence inside training. The
   role's challenger is the best candidate over all three blocks.
2. Validate (validation). The challenger and the core run on the validation period through the
   paper CLOB, the venue the portfolio will use. The out-of-sample evidence is the walk-forward
   blocks plus validation. Acceptance criteria, all required:
     - total out-of-sample excess return over the core > 0 (after fees, spread and slippage)
     - strength > 1: both t-statistics from hellabandz.agents.tournament.evidence_of_edge (per-bar
       excess over the core, and the intercept after regressing on the core, so it is not just holding
       less of a falling market) must exceed one standard error. Weight grows linearly from 0 at one
       standard error to full at two. (Design seeds 1-5 showed strategies funded below one standard
       error losing to the core on their test periods; this threshold was set from those seeds only.)
     - annualized turnover on validation <= MAX_TURNOVER_PER_YEAR x capital (a sanity cap; costs are
       already inside the returns)
   Accepted challengers are promoted with weight = 30% x that strength weight. Rejected ones stay rejected,
   and the role holds no capital. Capital nobody earned stays in the passive core.
3. The held-out test is not used by any of the above. hellabandz.trading.evaluate runs it once and
   counts the view in the registry.

The search is bounded (MAX_CANDIDATES per role, a time budget) and runs at low priority. It is not
re-run in response to recent losses; it runs when someone launches research.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hellabandz.agents.tournament import evidence_of_edge
from hellabandz.backtester import SnapshotProvider
from hellabandz.contracts import MICROS
from hellabandz.ingestion.dataset import load_snapshot
from hellabandz.strategies import BARS_PER_YEAR
from hellabandz.trading.coordinator import paper_budget
from hellabandz.trading.execution import VenueMode
from hellabandz.trading.instruments import instruments_for_dataset
from hellabandz.trading.marketdata import DataClass
from hellabandz.trading.registry import Registry
from hellabandz.trading.runtime import CoordinatedRuntime, RuntimeResult
from hellabandz.trading.strategies import RuntimeStrategy, StrategyVersion, make_version

INITIAL = 10_000 * MICROS
DEPLOYABLE = 9_000 * MICROS
ROLE_CAP = 0.30
MAX_CANDIDATES = 6
MAX_TURNOVER_PER_YEAR = 60.0
BLOCKS = 3

# role -> list of (card, params). Every value is inside the family's whitelisted bounds.
GRIDS: dict[str, list[tuple[str, dict]]] = {
    "trend": [("trend", {"fast": f, "slow": s}) for f, s in ((20, 100), (35, 200), (50, 300), (100, 500))],
    "mean_reversion": [("mean_reversion", {"lookback": lb, "entry_z": e, "exit_z": x})
                       for lb, e, x in ((30, 2.0, 0.0), (60, 2.0, 0.0), (60, 2.5, 0.5), (120, 2.5, 0.5))],
    # regime-aware defensive overlay (grid from the recorded proposal in data/fixtures/proposals/mean_reversion.json)
    "defensive": [("regime", {"regime_lookback": rl, "min_reversion": mr, "lookback": lb, "sell_z": sz, "rebuy_z": rz})
                  for rl, mr, lb, sz, rz in ((200, 0.015, 60, 1.0, 0.0), (200, 0.025, 100, 1.5, 0.0),
                                             (300, 0.015, 100, 1.0, -0.5), (300, 0.025, 60, 1.5, -0.5))]
                 + [("defensive", {"vol_lookback": 100, "trend_lookback": 400, "target_vol": 0.15})],
}


def data_class(meta: dict) -> DataClass:
    return DataClass.SYNTHETIC if "synthetic" in (meta.get("source", "") + meta.get("dataset_id", "")) else DataClass.HISTORICAL


def simulate(provider: SnapshotProvider, meta: dict, sleeves: list[tuple[str, StrategyVersion, int]], mode: VenueMode,
             start: int, end: int, run_id: str, keep_decisions: bool = False) -> RuntimeResult:
    venue = "paper_clob" if mode == VenueMode.PAPER_CLOB else "bar_sim"
    strats, capital = [], {}
    for sid, v, cap in sleeves:
        strats.append(RuntimeStrategy(StrategyVersion(sid, v.family, v.params, v.universe, v.version, v.card), venue))
        capital[sid] = cap
    rt = CoordinatedRuntime(run_id, mode, strats, capital, paper_budget(INITIAL), instruments_for_dataset(meta),
                            data_class(meta), INITIAL, keep_decisions=keep_decisions)
    rt.open()
    rt.warm(provider, start)
    return rt.run(provider, start, end)


def ret(r: RuntimeResult) -> float:
    return r.final_equity / r.initial_cash - 1


def bar_rets(r: RuntimeResult) -> np.ndarray:
    e = np.asarray([r.initial_cash] + r.equity, dtype=np.float64)
    return np.diff(e) / e[:-1]


def core_version(universe: list[str]) -> StrategyVersion:
    return make_version("core", "core", {}, universe)


@dataclass
class RoleOutcome:
    role: str
    challenger: StrategyVersion | None
    accepted: bool
    weight: float
    reasons: list[str]
    metrics: dict


def research(dataset_dir: Path, registry: Registry | None = None, budget_s: float = 900.0, progress=lambda *a: None) -> dict:
    if hasattr(os, "nice"):
        try:
            os.nice(10)  # never compete with the execution path for CPU
        except OSError:
            pass
    t, meta = load_snapshot(dataset_dir)
    p = SnapshotProvider(t)
    reg = registry or Registry()
    universe = sorted(meta["symbols"])
    sp = meta["splits"]
    tr0, tr1 = sp["train"]
    edges = [tr0 + (tr1 - tr0) * k // BLOCKS for k in range(BLOCKS + 1)]
    blocks = list(zip(edges[:-1], edges[1:]))
    core = core_version(universe)
    started = time.monotonic()
    core_train = [simulate(p, meta, [("core", core, DEPLOYABLE)], VenueMode.HISTORICAL_SIM, a, b, "fit-core") for a, b in blocks]
    core_rets = [bar_rets(c) for c in core_train]
    outcomes: list[RoleOutcome] = []
    for role, grid in GRIDS.items():
        cands = []
        for card, params in grid[:MAX_CANDIDATES]:
            if time.monotonic() - started > budget_s:
                break
            v = make_version(role, card, params, universe)
            reg.propose(role, v, source="coordinated-research grid")
            runs = [simulate(p, meta, [(role, v, DEPLOYABLE)], VenueMode.HISTORICAL_SIM, a, b, f"fit-{role}") for a, b in blocks]
            cands.append((v, [ret(r) - ret(c) for r, c in zip(runs, core_train)], [bar_rets(r) for r in runs]))
            progress("fit", role, len(cands))
        if not cands:
            outcomes.append(RoleOutcome(role, None, False, 0.0, ["TIME_BUDGET_EXHAUSTED"], {}))
            continue

        def pick(k: int):  # best mean excess on blocks [0, k); ties by version
            return max(cands, key=lambda c: (float(np.mean(c[1][:k])), c[0].version))
        wf = [(pick(k), k) for k in range(1, BLOCKS)]  # picked before block k, judged on block k
        wf_excess = [c[1][k] for c, k in wf]
        wf_rets = [c[2][k] for c, k in wf]
        v, train_ex, _ = pick(BLOCKS)
        tested = len(cands)
        va0, va1 = sp["validation"]
        cv = simulate(p, meta, [("core", core, DEPLOYABLE)], VenueMode.PAPER_CLOB, va0, va1, "val-core")
        sv = simulate(p, meta, [(role, v, DEPLOYABLE)], VenueMode.PAPER_CLOB, va0, va1, f"val-{role}")
        years = max(sv.cycles / BARS_PER_YEAR, 1e-9)
        edge = evidence_of_edge(np.concatenate(wf_rets + [bar_rets(sv)]),
                                np.concatenate([core_rets[k] for _, k in wf] + [bar_rets(cv)]))
        oos = sum(wf_excess) + ret(sv) - ret(cv)
        strength = min(edge["excess_t"], edge["alpha_t"])
        evidence_weight = min(max(strength - 1.0, 0.0), 1.0)
        m = {"train_block_excess": [round(x, 6) for x in train_ex], "walk_forward_excess": [round(x, 6) for x in wf_excess],
             "out_of_sample_excess": round(oos, 6), "validation_excess": round(ret(sv) - ret(cv), 6),
             "validation_return": round(ret(sv), 6), "core_validation_return": round(ret(cv), 6),
             "validation_turnover_per_year": round(sv.turnover / years, 2), "evidence": edge,
             "candidates_tested": tested}
        reasons = []
        if oos <= 0:
            reasons.append("NO_OUT_OF_SAMPLE_EXCESS_OVER_CORE")
        if evidence_weight <= 0:
            reasons.append("EDGE_BELOW_ONE_STANDARD_ERROR")
        if m["validation_turnover_per_year"] > MAX_TURNOVER_PER_YEAR:
            reasons.append("TURNOVER_TOO_HIGH")
        accepted = not reasons
        key = f"{role}:{v.version}"
        criteria = {"out_of_sample_excess_gt": 0, "min_t_statistic_gt": 1.0,
                    "max_turnover_per_year": MAX_TURNOVER_PER_YEAR}
        if reg.data["versions"][key]["status"] == "rejected":
            accepted, reasons = False, reasons or ["PREVIOUSLY_REJECTED"]
        else:
            reg.record_evaluation(key, m, criteria, accepted, reasons or ["MEETS_ALL_CRITERIA"])
        m["strength_t"] = round(strength, 4)
        weight = round(ROLE_CAP * evidence_weight, 4) if accepted else 0.0
        if accepted:
            reg.promote(role, key, weight)
        else:
            reg.demote(role, "challenger rejected; capital returns to the passive core")
        outcomes.append(RoleOutcome(role, v, accepted, weight, reasons or ["MEETS_ALL_CRITERIA"], m))
        progress("validated", role, accepted)
    return {"dataset_id": meta["dataset_id"], "dataset_sha256": meta["content_sha256"], "universe": universe,
            "roles": [{"role": o.role, "version": o.challenger and o.challenger.version, "family": o.challenger
                       and o.challenger.family.value, "params": o.challenger and o.challenger.param_dict,
                       "accepted": o.accepted, "weight": o.weight, "reasons": o.reasons, "metrics": o.metrics}
                      for o in outcomes],
            "seconds": round(time.monotonic() - started, 1)}


def portfolio_sleeves(reg: Registry, universe: list[str]) -> list[tuple[str, StrategyVersion, int]]:
    """Champions at their weights; the passive core holds the rest of the deployable capital."""
    sleeves = []
    used = 0
    for role, c in reg.champions().items():
        cap = int(DEPLOYABLE * c["weight"])
        if cap <= 0 or c["universe"] != universe:
            continue
        card = {"sma_crossover": "trend", "zscore_reversion": "mean_reversion", "vol_target": "defensive",
                "regime_reversion": "regime"}[c["family"]]
        sleeves.append((role, make_version(role, card, c["params"], c["universe"]), cap))
        used += cap
    sleeves.append(("core", core_version(universe), DEPLOYABLE - used))
    return sleeves


def summary_line(x: float) -> str:
    return f"{x:+.2%}" if math.isfinite(x) else "n/a"
