"""Allocation causality, real calendar cadence, accounting and frozen evaluation."""
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from hellabandz.contracts import MICROS, StrategyFamily
from hellabandz.strategies import validate_params
from hellabandz.trading.allocation import NY, AllocationState, session_date
from hellabandz.trading.evaluate import validate_frozen_registry
from hellabandz.trading.marketdata import DataClass, InstrumentView, MarketSnapshot
from hellabandz.trading.proposals import allocation_universe, validate_proposal
from hellabandz.trading.registry import Registry
from hellabandz.trading.research import CODE_VERSION, POLICY_VERSION, portfolio_sleeves
from hellabandz.trading.strategies import RuntimeStrategy, make_version
from tests.unit.test_coordinator import Harness, P, SYMS


def epoch(day, hour=10):
    return int(datetime.combine(day, time(hour), NY).timestamp())


def sessions(n=150):
    d, days = date(2024, 1, 2), []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def version(role="rotation", cadence=5):
    p = {"lookback_sessions": 20, "top_k": 3, "rebalance_sessions": cadence, "threshold": 0.0}
    if role == "risk_rotation":
        p["vol_sessions"] = 10
    return make_version("rotation", role, p, SYMS)


def histories(days):
    ts = np.asarray([epoch(d, 15) for d in days], dtype=np.int64)
    return {s: (ts, np.linspace(100, 100 + 10 * (i + 1), len(ts)), np.zeros(len(ts)), np.zeros(len(ts)))
            for i, s in enumerate(SYMS)}


def snapshot(day, prices=None, hour=10, skip=()):
    ts = epoch(day, hour)
    prices = prices or P
    return MarketSnapshot(100, ts, DataClass.SYNTHETIC,
                          {s: InstrumentView(s, 1, ts, int(p * MICROS), False, (p,), (p,), (p,), (ts,))
                           for s, p in prices.items() if s not in skip})


@pytest.mark.parametrize("role", ["rotation", "risk_rotation"])
def test_ranking_is_causal_and_warm_start_is_deterministic(role):
    days = sessions()
    h = histories(days[:100])
    a, b = AllocationState(version(role, cadence=1)), AllocationState(version(role, cadence=1))
    a.warm(h)
    b.warm(h)
    a.update(snapshot(days[100], {s: 1_000_000. for s in SYMS}))
    b.update(snapshot(days[100], {s: 1. for s in SYMS}))
    assert a.weights == b.weights  # current session movement cannot affect today's selection
    assert sum(a.weights.values()) == 10_000
    assert a.latest_signal_date == days[99].isoformat()
    if role == "rotation":
        assert a.weights[SYMS[0]] == 0  # weakest raw momentum excluded
    c = AllocationState(version(role, cadence=1))
    for sym, (ts, close, _, _) in h.items():
        for t, p in zip(ts, close):
            c.observe(sym, int(t), float(p))
    c.update(snapshot(days[100]))
    assert c.weights == a.weights


def test_weekly_calendar_does_not_rebalance_hourly_or_after_five_arbitrary_bars():
    days = sessions()
    a = AllocationState(version())
    a.warm(histories(days[:100]))
    # Monday holiday: first available session Tuesday determines the week's decision.
    tue = date(2024, 7, 9)
    a.update(snapshot(tue))
    token, weights = a.allocation_id, dict(a.weights)
    a.update(snapshot(tue, hour=15))
    a.update(snapshot(tue + timedelta(days=1), {s: 1. for s in SYMS}))
    assert (a.allocation_id, a.weights) == (token, weights)
    a.update(snapshot(tue + timedelta(days=6)))
    assert a.allocation_id != token
    assert session_date(epoch(date(2024, 3, 8))) == date(2024, 3, 8)
    assert session_date(epoch(date(2024, 3, 11))) == date(2024, 3, 11)  # DST
    assert epoch(date(2024, 3, 11)) - epoch(date(2024, 3, 8)) == 71 * 3600


def test_daily_missing_and_partial_sessions_use_only_observed_previous_dates():
    days = sessions()
    a = AllocationState(version(cadence=1))
    a.warm(histories(days[:100]))
    previous = a.decision_key
    a.update(snapshot(days[100], skip=[SYMS[0]]))
    assert a.decision_key == previous
    a.update(snapshot(days[100], hour=12))  # early close proxy; no seven-bar assumption
    token = a.allocation_id
    a.update(snapshot(days[101]))
    assert a.allocation_id != token
    assert a.latest_signal_date == days[100].isoformat()


def test_weekly_warm_start_replays_prior_decisions_instead_of_reranking_midweek():
    days = sessions(100)
    h = histories(days)
    warm, cold = AllocationState(version()), AllocationState(version())
    warm.warm(h)
    for i, day in enumerate(days):
        cold.update(snapshot(day, {s: float(h[s][1][i]) for s in SYMS}, hour=15))
    assert warm.weights == cold.weights
    assert warm.allocation_id == cold.allocation_id
    assert warm.latest_signal_date == cold.latest_signal_date


def vector(h, snap, weights, token="week1"):
    return [h.intent(snap, "rotation", s, weights[i]).model_copy(update={"weight_scope": "sleeve", "allocation_id": token})
            for i, s in enumerate(SYMS)]


def allocation_harness():
    h = Harness(capital={"rotation": 9000})
    h.active["rotation"] = replace(h.active["rotation"], weight_scope="sleeve")
    return h


@pytest.mark.parametrize("bad", ["oversum", "partial", "duplicate", "scope", "changed", "future"])
def test_invalid_vectors_are_rejected_atomically_without_orders_or_netting(bad):
    h = allocation_harness()
    snap = h.bar(P)
    good = vector(h, snap, [2500] * 4)
    if bad == "changed":
        h.decide(snap, good, execute=False)
    v = list(good)
    if bad == "oversum":
        v[0] = v[0].model_copy(update={"target_weight_bps": 5000})
    if bad == "partial":
        v.pop()
    if bad == "duplicate":
        v[-1] = v[0]
    if bad == "scope":
        v[0] = v[0].model_copy(update={"weight_scope": "slot"})
    if bad == "changed":
        v[0] = v[0].model_copy(update={"target_weight_bps": 0})
    if bad == "future":
        v[0] = v[0].model_copy(update={"decision_ts": snap.ts + 1})
    decisions = h.decide(snap, v, execute=False)
    assert all(d.order is None and not d.transfers for d in decisions.values())
    assert all(r.reasons == ["INVALID_ALLOCATION_VECTOR"] for d in decisions.values() for r in d.intents)


@pytest.mark.parametrize("unsupported", ["crypto", "unregistered"])
@pytest.mark.parametrize("cached", [False, True])
def test_unregistered_or_unsupported_member_rejects_entire_vector_before_target_cache(unsupported, cached):
    from hellabandz.trading.instruments import AssetClass
    h = allocation_harness()
    snap = h.bar(P)
    if cached:
        h.decide(snap, vector(h, snap, [2500] * 4), execute=False)
    original_allocations = dict(h.coord.state.allocations)
    original_targets = dict(h.coord.state.allocation_qty)
    if unsupported == "crypto":
        h.coord.instruments[SYMS[0]] = replace(h.coord.instruments[SYMS[0]], asset_class=AssetClass.CRYPTO)
    else:
        del h.coord.instruments[SYMS[0]]
    ds = h.decide(snap, vector(h, snap, [0, 3334, 3333, 3333], "week2"), execute=False)
    assert all(d.order is None and not d.transfers for d in ds.values())
    assert all(r.reasons == ["INVALID_ALLOCATION_VECTOR"] for d in ds.values() for r in d.intents)
    assert h.coord.state.allocations == original_allocations
    assert h.coord.state.allocation_qty == original_targets


def test_whole_sleeve_capital_rotates_and_quantities_do_not_trim_between_decisions():
    h = allocation_harness()
    snap = h.bar(P)
    ds = h.decide(snap, vector(h, snap, [2500] * 4))
    assert all(d.intents[0].requested_qty == 22 for d in ds.values())  # $2250, not the legacy $562 slot
    snap = h.bar(P)
    h.decide(snap, vector(h, snap, [2500] * 4))
    snap = h.bar({s: 200. for s in SYMS})
    ds = h.decide(snap, vector(h, snap, [2500] * 4))
    assert all(d.order is None for d in ds.values())
    assert all(d.intents[0].requested_qty == 22 for d in ds.values())
    ds = h.decide(snap, vector(h, snap, [0, 3334, 3333, 3333], "week2"))
    assert ds[SYMS[0]].order.side == "SELL"
    assert ds[SYMS[0]].order.qty == 22
    assert ds[SYMS[1]].intents[0].requested_qty == 15
    h.check()
    # At unchanged prices, selling one constituent actually releases capital
    # that subsequent cycles deploy into the remaining three, under original caps.
    rotated = allocation_harness()
    for _ in range(3):
        snap = rotated.bar(P)
        rotated.decide(snap, vector(rotated, snap, [2500] * 4))
    initial = {s: rotated.pos(s) for s in SYMS}
    assert all(0 < q <= 22 for q in initial.values())  # original tech group cap can bind
    for _ in range(6):
        snap = rotated.bar(P)
        rotated.decide(snap, vector(rotated, snap, [0, 3334, 3333, 3333], "week2"))
    assert rotated.pos(SYMS[0]) == 0
    assert all(rotated.pos(s) > initial[s] for s in SYMS[1:])
    rotated.check()


def test_allocation_model_proposals_and_universe_selection_are_bounded():
    from hellabandz.trading.instruments import SECTOR_ETFS
    universe = sorted([*SYMS, *SECTOR_ETFS])
    assert allocation_universe(universe) == list(SECTOR_ETFS)
    raw = {"proposal_id": "p", "agent_id": "a", "family": "relative_strength", "universe": list(SECTOR_ETFS),
           "param_grid": {"lookback_sessions": [63], "top_k": [4], "rebalance_sessions": [1, 5], "threshold": [0]},
           "rationale": "sector persistence"}
    assert len(validate_proposal(raw, "trend", universe)) == 2
    with pytest.raises(ValueError):
        validate_params(StrategyFamily.RELATIVE_STRENGTH, {**version().param_dict, "rebalance_sessions": 2})
    with pytest.raises(ValueError):
        validate_params(StrategyFamily.RELATIVE_STRENGTH, {**version().param_dict, "top_k": 3.5})


def test_exact_return_attribution_reconciles_both_execution_modes():
    from hellabandz.backtester import SnapshotProvider
    from hellabandz.trading.execution import VenueMode
    from hellabandz.trading.research import DEPLOYABLE, core_version, simulate
    from tests.unit.test_adaptive_research import market
    t, meta = market()
    for mode in (VenueMode.PAPER_CLOB, VenueMode.HISTORICAL_SIM):
        r = simulate(SnapshotProvider(t), meta, [("core", core_version(["SPY"]), DEPLOYABLE)], mode,
                     *meta["splits"]["test"], "attribution", keep_telemetry=True)
        a = r.return_attribution
        assert a["net_pnl"] == r.final_equity - r.initial_cash
        assert a["cycle_residual"] == a["reconciliation_residual"] == 0
        assert sum(x["fees"] for x in a["instruments"].values()) == r.fees
        assert sum(x["modeled_slippage"] for x in a["instruments"].values()) == r.slippage
        assert len(r.telemetry) == r.cycles


def test_frozen_registry_checks_chronology_code_and_version_and_does_not_refit(tmp_path):
    reg = Registry(tmp_path / "frozen.json")
    v = version()
    reg.data["research_state"] = {"status": "complete", "policy_version": POLICY_VERSION,
                                  "code_version": CODE_VERSION, "universe": sorted(SYMS),
                                  "splits": {"validation": [10, 20]}, "frozen_challengers": {"rotation": v.describe()}}
    meta = {"symbols": SYMS, "interval_s": 3600}
    assert validate_frozen_registry(reg, meta, 20) is reg.data["research_state"]
    reg.data["research_state"]["data_convention"] = {"adjustment": "all", "interval_s": 3600, "unit_version": None}
    with pytest.raises(ValueError, match="adjustments"):
        validate_frozen_registry(reg, meta, 20)
    meta["adjustment"] = "all"
    assert validate_frozen_registry(reg, meta, 20)
    with pytest.raises(ValueError, match="after"):
        validate_frozen_registry(reg, meta, 19)
    reg.data["research_state"]["frozen_challengers"]["rotation"]["version"] = "tampered"
    with pytest.raises(ValueError, match="version"):
        validate_frozen_registry(reg, meta, 20)


def test_duplicate_champion_versions_never_receive_capital_twice(tmp_path):
    reg = Registry(tmp_path / "duplicates.json")
    v = version()
    for role in ("trend", "defensive"):
        key = reg.propose(role, v, "test")
        reg.record_evaluation(key, {}, {}, True, [])
        reg.promote(role, key, .3)
    sleeves = portfolio_sleeves(reg, sorted(SYMS))
    assert len([s for s, _, _ in sleeves if s != "core"]) == 1


def test_frozen_evaluator_uses_registry_without_research_and_honors_historical_reuse(monkeypatch, tmp_path):
    from hellabandz.trading import evaluate, research
    from tests.unit.test_adaptive_research import harness, market
    table, meta = market()
    meta["historical_periods_previously_inspected"] = True
    harness(monkeypatch, table, meta)
    reg = Registry(tmp_path / "registry.json")
    research.research(tmp_path, reg, 20)
    champions = dict(reg.data["champions"])
    runs = len(reg.data["research_runs"])
    monkeypatch.setattr(evaluate, "load_snapshot", lambda _: (table, meta))
    monkeypatch.setattr(evaluate, "research", lambda *_: pytest.fail("frozen evaluation refitted"))
    out = evaluate.evaluate_dataset(tmp_path, reg.path, frozen_registry=True)
    assert out["evaluation_mode"] == "frozen_registry_no_refit"
    assert out["holdout_views"] == 1 and not out["holdout_untouched"]
    current = Registry(reg.path)
    assert current.data["champions"] == champions
    assert len(current.data["research_runs"]) == runs
    assert out["rows"]["coordinated_portfolio"]["return_attribution"]["reconciliation_residual"] == 0


def test_exact_attribution_handles_buys_sells_and_embedded_slippage_without_double_subtraction():
    from hellabandz.trading.diagnostics import ReturnAttribution
    a = ReturnAttribution()
    fills = [{"symbol": "SPY", "side": "BUY", "qty": 2, "px": 111, "fee": 1, "slippage": 2},
             {"symbol": "SPY", "side": "SELL", "qty": 5, "px": 109, "fee": 1, "slippage": 5}]
    a.record({"SPY": 10}, {"SPY": 100}, {"SPY": 110}, fills, 91)
    report = a.report(91, 500, 1000)
    assert report["instruments"]["SPY"]["mark_pnl"] == 100
    assert report["instruments"]["SPY"]["execution_pnl_at_close"] == -7
    assert report["net_pnl"] == 91
    assert report["cycle_residual"] == report["reconciliation_residual"] == 0


def test_allocation_ties_and_nonfinite_scores_are_deterministic_and_safe():
    days = sessions(30)
    a = AllocationState(version(cadence=1))
    h = histories(days)
    close = h[SYMS[0]][1]
    a.warm({s: (v[0], close, v[2], v[3]) for s, v in h.items()})
    a.update(snapshot(days[-1] + timedelta(days=1)))
    assert {s for s, w in a.weights.items() if w} == set(sorted(SYMS)[:3])
    harness = allocation_harness()
    snap = harness.bar(P)
    v = vector(harness, snap, [2500] * 4)
    v[0] = v[0].model_copy(update={"score": float("nan")})
    ds = harness.decide(snap, v, execute=False)
    assert all(d.order is None for d in ds.values())


def test_legacy_tournament_and_backtester_intentionally_reject_allocation_families():
    from hellabandz.agents.tournament import AGENTS, expand_grid
    from hellabandz.contracts import StrategyProposal, StrategySpec
    from hellabandz.backtester import Slot, run_backtest
    from hellabandz.strategies import ALLOCATION_FAMILIES
    assert all(not set(cfg["families"]) & ALLOCATION_FAMILIES for cfg in AGENTS.values())
    prop = StrategyProposal(proposal_id="p", agent_id="a", family="relative_strength", universe=SYMS,
                            param_grid={k: [x] for k, x in version().param_dict.items()}, rationale="test")
    with pytest.raises(ValueError, match="coordinated"):
        expand_grid(prop)
    spec = StrategySpec(strategy_id="s", agent_id="a", family=prop.family, params=version().param_dict, universe=SYMS)
    with pytest.raises(ValueError, match="CoordinatedRuntime"):
        run_backtest(None, [Slot(spec, 1000)], 0, 10)
