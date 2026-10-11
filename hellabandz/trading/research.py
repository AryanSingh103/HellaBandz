"""Development proposal -> optimize -> critique -> freeze -> validate -> paper promotion.

Only the first training third reaches agents and optimization feedback. Frozen
challengers see later training thirds and validation once. Test bars are excluded
from the research provider. Execution uses reviewed frozen code, never an LLM.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path

import numpy as np

from hellabandz.agents.research import train_summary
from hellabandz.agents.tournament import evidence_of_edge
from hellabandz.backtester import SnapshotProvider
from hellabandz.contracts import MICROS
from hellabandz.ingestion.dataset import load_snapshot
from hellabandz.strategies import BARS_PER_YEAR
from hellabandz.trading.coordinator import paper_budget
from hellabandz.trading.execution import VenueMode
from hellabandz.trading.instruments import COORDINATED_ASSET_CLASSES, instruments_for_dataset
from hellabandz.trading.proposals import (ROLE_CARDS, allocation_universe, defensive_challenger_proposals,
                                         house_proposals, llm_proposals, mutations)
from hellabandz.trading.registry import Registry
from hellabandz.trading.runtime import CoordinatedRuntime, RuntimeResult
from hellabandz.trading.strategies import CODE_VERSION, FAMILY_CARDS, RuntimeStrategy, StrategyVersion, make_version

INITIAL = 10_000 * MICROS
DEPLOYABLE = 9_000 * MICROS
ROLE_CAP = 0.30
MAX_CANDIDATES = 8
MAX_TURNOVER_PER_YEAR = 60.0
BLOCKS = 3
POLICY_VERSION = "adaptive-allocation-3"
RESEARCH_PROFILES = ("broad", "defensive_challenger")
CRITERIA = {"net_return_excess_gt": 0.001, "sharpe_excess_gt": 0.05,
            "sortino_excess_gt": 0.0, "drawdown_excess_lte": 0.0,
            "max_turnover_per_year": MAX_TURNOVER_PER_YEAR, "max_cost_fraction_per_year": 0.02,
            "cost_stress_multiplier": 2.0,
            "evidence": "max(1.96, sqrt(2*log(development_trials))); heuristic, not confidence guarantee"}


class ResearchStopped(RuntimeError):
    pass


def data_class(meta: dict):
    from hellabandz.trading.marketdata import DataClass
    return DataClass.SYNTHETIC if "synthetic" in (meta.get("source", "") + meta.get("dataset_id", "")) else DataClass.HISTORICAL


def simulate(provider: SnapshotProvider, meta: dict, sleeves: list[tuple[str, StrategyVersion, int]], mode: VenueMode,
             start: int, end: int, run_id: str, keep_decisions: bool = False, check=None,
             keep_telemetry: bool = False) -> RuntimeResult:
    if check:
        check()
    venue = "paper_clob" if mode == VenueMode.PAPER_CLOB else "bar_sim"
    strats, capital = [], {}
    for sid, v, cap in sleeves:
        strats.append(RuntimeStrategy(StrategyVersion(sid, v.family, v.params, v.universe, v.version, v.card), venue))
        capital[sid] = cap
    rt = CoordinatedRuntime(run_id, mode, strats, capital, paper_budget(INITIAL), instruments_for_dataset(meta),
                            data_class(meta), INITIAL, keep_decisions=keep_decisions, keep_telemetry=keep_telemetry)
    rt.open()
    rt.warm(provider, start)
    if check:
        check()
    return rt.run(provider, start, end, check=check)


def ret(r: RuntimeResult) -> float:
    return r.final_equity / r.initial_cash - 1


def bar_rets(r: RuntimeResult) -> np.ndarray:
    e = np.asarray([r.initial_cash] + r.equity, dtype=np.float64)
    return np.diff(e) / e[:-1]


def core_version(universe: list[str]) -> StrategyVersion:
    return make_version("core", "core", {}, universe)


def research_metrics(r: RuntimeResult) -> dict:
    e = np.asarray([r.initial_cash] + r.equity, dtype=np.float64)
    rr = bar_rets(r)
    sd = float(rr.std()) if len(rr) else 0.0
    downside = math.sqrt(float(np.mean(np.minimum(rr, 0) ** 2))) if len(rr) else 0.0
    years = max(len(rr) / BARS_PER_YEAR, 1e-9)
    return {"net_return": ret(r), "sharpe": float(rr.mean() / sd * math.sqrt(BARS_PER_YEAR)) if sd > 0 else 0.0,
            "sortino": float(rr.mean() / max(downside, 1e-12) * math.sqrt(BARS_PER_YEAR)) if len(rr) else 0.0,
            "max_drawdown": float(((np.maximum.accumulate(e) - e) / np.maximum.accumulate(e)).max()),
            "turnover_per_year": r.turnover / years, "turnover": r.turnover,
            "cost_fraction": (r.fees + r.slippage) / r.initial_cash,
            "cost_fraction_per_year": (r.fees + r.slippage) / r.initial_cash / years,
            "fills": r.fills, "cycles": r.cycles, "ledger_digest": r.ledger_digest,
            "reconciliation_breaks": len(r.reconciliation_breaks), "ledger_rejections": r.ledger_rejections}


def metric_gates(candidate: dict, baseline: dict) -> list[str]:
    """Fees/turnover are bounded, not claimed lower than buy/hold."""
    numeric = ("net_return", "sharpe", "sortino", "max_drawdown", "turnover_per_year", "cost_fraction_per_year", "cost_fraction")
    if any(not math.isfinite(candidate[k]) or not math.isfinite(baseline[k]) for k in numeric):
        return ["NONFINITE_METRICS"]
    reasons = []
    if candidate["cycles"] < 30 or candidate["fills"] < 1:
        reasons.append("INSUFFICIENT_OBSERVATIONS_OR_FILLS")
    if candidate["net_return"] - baseline["net_return"] <= CRITERIA["net_return_excess_gt"]:
        reasons.append("RETURN_NOT_ABOVE_BASELINE")
    if candidate["sharpe"] - baseline["sharpe"] <= CRITERIA["sharpe_excess_gt"]:
        reasons.append("SHARPE_NOT_ABOVE_BASELINE")
    if candidate["sortino"] <= baseline["sortino"]:
        reasons.append("SORTINO_NOT_ABOVE_BASELINE")
    if candidate["max_drawdown"] > baseline["max_drawdown"] + 1e-12:
        reasons.append("DRAWDOWN_WORSE_THAN_BASELINE")
    if candidate["turnover_per_year"] > MAX_TURNOVER_PER_YEAR:
        reasons.append("TURNOVER_TOO_HIGH")
    if candidate["cost_fraction_per_year"] > CRITERIA["max_cost_fraction_per_year"]:
        reasons.append("COST_BUDGET_EXCEEDED")
    if candidate["net_return"] - candidate["cost_fraction"] <= baseline["net_return"] - baseline["cost_fraction"]:
        reasons.append("NO_EXCESS_UNDER_DOUBLE_COSTS")
    if candidate["reconciliation_breaks"] or candidate["ledger_rejections"]:
        reasons.append("EXECUTION_INTEGRITY_FAILURE")
    return reasons


def development_score(metrics: dict, baseline: dict) -> float:
    """Only development results enter ranking or critique."""
    return (metrics["net_return"] - baseline["net_return"]
            + 0.02 * (metrics["sharpe"] - baseline["sharpe"])
            - max(0, metrics["max_drawdown"] - baseline["max_drawdown"])
            - 0.001 * max(0, metrics["turnover_per_year"] - MAX_TURNOVER_PER_YEAR))


def development_consistency(candidate, baseline) -> list[float]:
    """Two chronological halves of the existing development path; no extra replay."""
    def parts(result):
        e = np.asarray([result.initial_cash] + result.equity, dtype=np.float64)
        middle = len(result.equity) // 2
        return [float(e[middle] / e[0] - 1), float(e[-1] / e[middle] - 1)]
    return [x - y for x, y in zip(parts(candidate), parts(baseline))]


def research_current(reg: Registry, meta: dict) -> bool:
    rec = reg.data.get("research_state", {})
    return (rec.get("status") == "complete" and rec.get("policy_version") == POLICY_VERSION
            and rec.get("profile", "broad") == "broad"
            and rec.get("code_version") == CODE_VERSION and rec.get("dataset_sha256") == meta["content_sha256"])


def research(dataset_dir: Path, registry: Registry | None = None, budget_s: float = 900.0, progress=lambda *a: None,
             *, rounds: int = 3, max_candidates: int = MAX_CANDIDATES, use_llm: bool = False,
             stop_requested=lambda: False, profile: str = "broad") -> dict:
    reg = registry or Registry()
    with reg.research_lock():
        return _research(dataset_dir, reg, budget_s, progress, rounds=rounds, max_candidates=max_candidates,
                         use_llm=use_llm, stop_requested=stop_requested, profile=profile)


def _research(dataset_dir: Path, registry: Registry, budget_s: float, progress,
              *, rounds: int, max_candidates: int, use_llm: bool, stop_requested, profile: str = "broad") -> dict:
    """Serial low-priority research; deadline checked inside every simulation cycle.

    A stopped run leaves passive fallback, never a partly validated portfolio.
    Explicit calls re-run research. Startup reuses completed current-policy results.
    """
    if not math.isfinite(budget_s) or budget_s <= 0 or not 1 <= rounds <= 5 or not 1 <= max_candidates <= 16:
        raise ValueError("positive finite budget; rounds 1..5; candidates per role 1..16 required")
    if profile not in RESEARCH_PROFILES:
        raise ValueError(f"research profile must be one of {RESEARCH_PROFILES}")
    if profile == "defensive_challenger":
        if use_llm:
            raise ValueError("defensive_challenger is a fixed reviewed grid; model proposals are not supported")
        rounds, max_candidates = 1, 32
    roles = ("defensive",) if profile == "defensive_challenger" else tuple(ROLE_CARDS)
    started = time.monotonic()
    deadline = started + budget_s

    def check():
        if time.monotonic() >= deadline:
            raise ResearchStopped("TIME_BUDGET_EXHAUSTED")
        if stop_requested():
            raise ResearchStopped("STOP_REQUESTED")

    if hasattr(os, "nice"):
        try:
            os.nice(max(0, 10 - os.nice(0)))
        except OSError:
            pass
    t, meta = load_snapshot(dataset_dir)
    if meta.get("interval_s") != 3600:
        raise ValueError("research metrics currently require hourly stock/ETF bars")
    if any(i.asset_class not in COORDINATED_ASSET_CLASSES for i in instruments_for_dataset(meta).values()):
        raise ValueError("research supports stocks and ETFs only")
    sp = meta["splits"]
    tr0, tr1 = sp["train"]
    va0, va1 = sp["validation"]
    te0, te1 = sp["test"]
    if not tr0 < tr1 <= va0 < va1 <= te0 < te1:
        raise ValueError("train, validation and test must be chronological and disjoint")
    development = t.select(t.ts < va1)  # test physically excluded
    p = SnapshotProvider(development)
    universe = sorted(meta["symbols"])
    reg = registry or Registry()
    for role in list(reg.champions()):
        reg.demote(role, "new research must pass current dataset and whole-portfolio gates")
    run_no = len(reg.data.get("research_runs", [])) + 1
    out = {"run": run_no, "policy_version": POLICY_VERSION, "code_version": CODE_VERSION, "profile": profile,
           "dataset_id": meta["dataset_id"], "dataset_sha256": meta["content_sha256"], "universe": universe,
           "data_convention": {k: meta.get(k) for k in ("interval_s", "schema_version", "unit_version", "adjustment", "quote_currency", "source")},
           "allocation_universe": allocation_universe(universe),
           "allocation_universe_policy": "available sector ETFs when at least3 registered; otherwise registered fallback universe",
           "historical_periods_previously_inspected": bool(meta.get("historical_periods_previously_inspected", False)),
           "development_score_formula": "return_excess + .02*sharpe_excess - positive_drawdown_excess "
                                        "- .001*positive_turnover_above60 + .25*minimum_two_half_return_excess",
           "baseline": "equal-weight buy-and-hold basket, same capital/venue/fees", "status": "running",
           "budget": {"seconds": budget_s, "rounds": rounds, "candidates_per_role": max_candidates, "workers": 1},
           "splits": sp, "criteria": CRITERIA, "experiments": [], "proposals": [], "roles": [],
           "validation_views": 0, "test_used_for_selection": False,
           "limitations": ["Paper synthetic OHLCV depth, not exchange liquidity.",
                           "Search-adjusted evidence is a heuristic, not proof of significance.",
                           "Validation reuse and selection can overfit; unseen future data must confirm an edge."]}
    reg.data["research_state"] = out
    reg._save()
    edges = [tr0 + (tr1 - tr0) * k // BLOCKS for k in range(BLOCKS + 1)]
    blocks = list(zip(edges[:-1], edges[1:]))
    core = core_version(universe)

    def run(v, a, b, mode, label):
        return simulate(p, meta, [(v.strategy_id, v, DEPLOYABLE)], mode, a, b, label, check=check)

    try:
        check()
        summary = train_summary(SnapshotProvider(t.select(t.ts < edges[1])), edges[1], meta)
        out["proposal_summary"] = summary
        out["proposal_data_end_exclusive"] = edges[1]
        cb = run(core, *blocks[0], VenueMode.HISTORICAL_SIM, "development-core")
        cbm = research_metrics(cb)
        pools = {}
        for role in roles:
            check()
            pools[role] = (defensive_challenger_proposals(universe) if profile == "defensive_challenger"
                           else house_proposals(role, universe, summary))
            if use_llm:
                extra, record = llm_proposals(role, universe, summary, min(10, max(0.1, deadline - time.monotonic())))
                out["proposals"].append(record)
                # Every house family is tested before additive model candidates. Limit
                # model seeds so feedback rounds retain room in the global role budget.
                pools[role] += extra[:2]
        scored = {r: [] for r in roles}
        seen = {r: set() for r in roles}
        for round_no in range(rounds):
            for role in roles:
                candidates = pools[role] if round_no == 0 else []
                if round_no:
                    leaders = sorted(scored[role], key=lambda x: (-x[0], x[1].version.version))
                    families = set()
                    for _, proposal, m in leaders:
                        if proposal.version.family in families:
                            continue
                        families.add(proposal.version.family)
                        candidates.extend(mutations(proposal, m, cbm, round_no))
                remaining = max_candidates - len(scored[role])
                slots = min(remaining, max(1, math.ceil(remaining / (rounds - round_no))))
                if round_no == 0:
                    slots = min(remaining, max(slots, len(pools[role])))
                for proposal in candidates:
                    v = proposal.version
                    if v.version in seen[role]:
                        continue
                    if slots <= 0:
                        break
                    check()
                    seen[role].add(v.version)
                    key = reg.propose(role, v, proposal.source)
                    r = run(v, *blocks[0], VenueMode.HISTORICAL_SIM, f"development-{role}")
                    m = research_metrics(r)
                    consistency = development_consistency(r, cb)
                    score = development_score(m, cbm) + .25 * min(consistency)
                    scored[role].append((score, proposal, m))
                    out["experiments"].append({"role": role, "key": key, "version": v.version,
                                               "family": v.family.value, "params": v.param_dict,
                                               "universe": list(v.universe), "development_half_excess": consistency,
                                               "round": round_no, "parent": proposal.parent, "source": proposal.source,
                                               "hypothesis": proposal.hypothesis, "feedback": proposal.feedback,
                                               "split": "development", "metrics": m, "score": score})
                    slots -= 1
                    reg._save()
                    progress("optimized", role, len(scored[role]))
        frozen = {r: max(c, key=lambda x: (x[0], x[1].version.version))[1].version for r, c in scored.items() if c}
        out["frozen_challengers"] = {r: v.describe() for r, v in frozen.items()}
        out["distinct_configurations"] = len({e["version"] for e in out["experiments"]})
        out["distinct_frozen_challengers"] = len({v.version for v in frozen.values()})
        trials = len(out["experiments"])
        threshold = max(1.96, math.sqrt(2 * math.log(max(trials, 1))))
        out["evidence_threshold"] = threshold
        validation_key = development.content_hash()
        counts = reg.data.setdefault("validation_views", {})
        counts[validation_key] = counts.get(validation_key, 0) + 1
        out["validation_views"] = counts[validation_key]
        out["validation_previously_inspected"] = bool(meta.get("historical_periods_previously_inspected")) or counts[validation_key] > 1 or any(
            h["event"] in ("accepted", "rejected") for h in reg.data["history"])
        reg._save()
        core_wf = [run(core, a, b, VenueMode.HISTORICAL_SIM, "walkforward-core") for a, b in blocks[1:]]
        cv = run(core, va0, va1, VenueMode.PAPER_CLOB, "validation-core")
        cvm = research_metrics(cv)
        out["validation_baseline"] = cvm
        proposed_sleeves = []
        funded_versions = set()
        for role, v in frozen.items():
            check()
            wf = [run(v, a, b, VenueMode.HISTORICAL_SIM, f"walkforward-{role}") for a, b in blocks[1:]]
            sv = run(v, va0, va1, VenueMode.PAPER_CLOB, f"validation-{role}")
            m = research_metrics(sv)
            ex = [ret(s) - ret(c) for s, c in zip(wf, core_wf)]
            edge = evidence_of_edge(np.concatenate([bar_rets(s) for s in wf] + [bar_rets(sv)]),
                                    np.concatenate([bar_rets(c) for c in core_wf] + [bar_rets(cv)]))
            strength = min(edge["excess_t"], edge["alpha_t"])
            reasons = metric_gates(m, cvm)
            if sum(ex) <= 0 or sum(x > 0 for x in ex) < 1:
                reasons.append("NO_WALK_FORWARD_EXCESS_OVER_CORE")
            if not math.isfinite(strength) or strength <= threshold:
                reasons.append("EDGE_BELOW_SEARCH_ADJUSTED_THRESHOLD")
            key = f"{role}:{v.version}"
            if reg.data["versions"][key]["status"] == "rejected":
                reasons.append("PREVIOUSLY_REJECTED")
            if not reasons and v.version in funded_versions:
                reasons.append("DUPLICATE_FUNDED_VERSION")
            weight = ROLE_CAP if not reasons else 0.0
            m.update(validation_return=m["net_return"], core_validation_return=cvm["net_return"],
                     validation_excess=m["net_return"] - cvm["net_return"], validation_turnover_per_year=m["turnover_per_year"],
                     walk_forward_excess=ex, out_of_sample_excess=sum(ex) + ret(sv) - ret(cv),
                     evidence=edge, strength_t=strength, candidates_tested=len(scored[role]))
            out["roles"].append({"role": role, "version": v.version, "family": v.family.value,
                                 "universe": list(v.universe),
                                 "params": v.param_dict, "accepted": not reasons, "weight": weight,
                                 "reasons": reasons, "metrics": m})
            if weight:
                proposed_sleeves.append((role, v, int(DEPLOYABLE * weight)))
                funded_versions.add(v.version)
            progress("validated", role, not reasons)
        portfolio_reasons = []
        if proposed_sleeves:
            proposed_sleeves.append(("core", core, DEPLOYABLE - sum(c for _, _, c in proposed_sleeves)))
            portfolio = simulate(p, meta, proposed_sleeves, VenueMode.PAPER_CLOB, va0, va1,
                                 "validation-portfolio", check=check)
            pm = research_metrics(portfolio)
            portfolio_reasons = metric_gates(pm, cvm)
            out["portfolio_validation"] = {"metrics": pm, "baseline": cvm, "reasons": portfolio_reasons,
                                           "accepted": not portfolio_reasons}
        else:
            out["portfolio_validation"] = {"accepted": False, "reasons": ["NO_ELIGIBLE_ACTIVE_STRATEGIES"]}
        check()
        for role in out["roles"]:
            if role["accepted"] and portfolio_reasons:
                role.update(accepted=False, weight=0.0, reasons=["COMBINED_PORTFOLIO_GATE_FAILED", *portfolio_reasons])
            key = f"{role['role']}:{role['version']}"
            reg.record_evaluation(key, role["metrics"], {**CRITERIA, "evidence_threshold": threshold,
                                                       "research_policy": POLICY_VERSION}, role["accepted"],
                                  role["reasons"] or ["MEETS_INDIVIDUAL_AND_PORTFOLIO_CRITERIA"])
            if role["accepted"]:
                reg.promote(role["role"], key, role["weight"])
        out["status"] = "complete"
    except ResearchStopped as exc:
        out["status"] = "stopped"
        out["stop_reason"] = str(exc)
        for role in list(reg.champions()):
            reg.demote(role, "research stopped before completing promotion")
        for role in out["roles"]:
            role.update(accepted=False, weight=0.0, reasons=[str(exc)])
    except Exception:
        out["status"] = "failed"
        reg.data["research_state"] = out
        reg._save()
        raise
    out["seconds"] = round(time.monotonic() - started, 3)
    out["candidates_tested"] = len(out["experiments"])
    reg.data["research_state"] = out
    reg.data.setdefault("research_runs", []).append(json.loads(json.dumps(out)))
    reg._save()
    return out


def portfolio_sleeves(reg: Registry, universe: list[str]) -> list[tuple[str, StrategyVersion, int]]:
    sleeves = []
    used = 0
    versions = set()
    state = reg.data.get("research_state")
    champions = reg.champions() if state is None or state.get("status") == "complete" else {}
    for role, c in champions.items():
        cap = int(DEPLOYABLE * c["weight"])
        if cap <= 0 or not set(c["universe"]).issubset(universe) or used + cap > DEPLOYABLE or c["version"] in versions:
            continue
        v = make_version(role, FAMILY_CARDS[c["family"]], c["params"], c["universe"])
        if v.version != c["version"]:
            continue  # historical runtime code cannot silently become a current champion
        sleeves.append((role, v, cap))
        versions.add(c["version"])
        used += cap
    sleeves.append(("core", core_version(universe), DEPLOYABLE - used))
    return sleeves


def summary_line(x: float) -> str:
    return f"{x:+.2%}" if math.isfinite(x) else "n/a"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, default=Path("data/fixtures/synthetic_1h"))
    ap.add_argument("--registry", type=Path, default=Path("data/registry/adaptive.json"))
    ap.add_argument("--budget-s", type=float, default=600)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--max-candidates", type=int, default=MAX_CANDIDATES)
    ap.add_argument("--llm-proposals", action="store_true")
    ap.add_argument("--profile", choices=RESEARCH_PROFILES, default="broad")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    report = research(args.dataset, Registry(args.registry), args.budget_s, rounds=args.rounds,
                      max_candidates=args.max_candidates, use_llm=args.llm_proposals, profile=args.profile)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps({k: report[k] for k in ("status", "seconds", "candidates_tested", "roles")}, allow_nan=False))


if __name__ == "__main__":
    main()
