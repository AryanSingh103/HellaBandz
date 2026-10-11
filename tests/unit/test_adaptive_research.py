"""Research safety and selection contracts with deterministic controlled market results."""
from types import SimpleNamespace

import numpy as np
import pytest

from hellabandz.contracts import StrategyFamily
from hellabandz.ingestion.dataset import BarTable
from hellabandz.trading import research as module
from hellabandz.trading.proposals import house_proposals, mutations, validate_proposal
from hellabandz.trading.registry import Registry


def test_defensive_challenger_tests_complete_grid_before_freezing(monkeypatch, tmp_path):
    calls = harness(monkeypatch)
    reg = Registry(tmp_path / "challenger.json")
    report = module.research(tmp_path, reg, 20, profile="defensive_challenger")
    experiments = report["experiments"]
    assert report["status"] == "complete"
    assert report["profile"] == "defensive_challenger"
    assert report["budget"]["rounds"] == 1
    assert report["budget"]["candidates_per_role"] == 32
    assert len(experiments) == report["distinct_configurations"] == 32
    assert {e["role"] for e in experiments} == {"defensive"}
    assert {e["family"] for e in experiments} == {"regime_reversion", "regime_fixed_units"}
    assert {e["params"]["sell_z"] for e in experiments} == {1.5, 2.5}
    assert {e["params"]["rebuy_z"] for e in experiments} == {0.0, .5}
    assert {e["params"]["lookback"] for e in experiments} == {100.0, 200.0}
    assert {e["params"]["min_reversion"] for e in experiments} == {.025, .075}
    assert len(report["frozen_challengers"]) == 1
    last_development = max(i for i, c in enumerate(calls) if c[0].startswith("development-"))
    first_validation = min(i for i, c in enumerate(calls) if c[0].startswith(("walkforward-", "validation-")))
    assert last_development < first_validation
    assert not module.research_current(reg, market()[1])  # focused grid cannot replace app-wide research


def test_defensive_grid_selection_ignores_validation_and_test_changes(monkeypatch, tmp_path):
    table, meta = market()
    harness(monkeypatch, table, meta)
    a = module.research(tmp_path, Registry(tmp_path / "a.json"), 20, profile="defensive_challenger")
    table.close[table.ts >= meta["splits"]["validation"][0]] *= 50
    meta["content_sha256"] = table.content_hash()
    harness(monkeypatch, table, meta)
    b = module.research(tmp_path, Registry(tmp_path / "b.json"), 20, profile="defensive_challenger")
    for key in ("proposal_summary", "experiments", "frozen_challengers"):
        assert a[key] == b[key]


@pytest.mark.parametrize("kwargs", [{"profile": "invalid"}, {"profile": "defensive_challenger", "use_llm": True}])
def test_invalid_focused_research_settings_fail_before_loading_data(tmp_path, kwargs):
    with pytest.raises(ValueError):
        module.research(tmp_path / "nonexistent", Registry(tmp_path / "r.json"), 20, **kwargs)


def market():
    ts = np.arange(1800, dtype=np.int64) * 3600 + 1_600_000_000
    px = np.linspace(100, 150, len(ts)).astype(np.int64) * 1_000_000
    table = BarTable(np.array(["SPY"] * len(ts), dtype=object), ts, px, px + 100_000, px - 100_000,
                     px, np.full(len(ts), 10000, dtype=np.int64))
    meta = {"dataset_id": "synthetic-research-test", "content_sha256": table.content_hash(), "symbols": ["SPY"],
            "source": "synthetic", "interval_s": 3600,
            "splits": {"train": [int(ts[0]), int(ts[1080])], "validation": [int(ts[1080]), int(ts[1440])],
                       "test": [int(ts[1440]), int(ts[-1]) + 1]}}
    return table, meta


def metric(good=False):
    return {"net_return": .03 if good else .01, "sharpe": 2.0 if good else 1.0,
            "sortino": 3.0 if good else 2.0, "max_drawdown": .04 if good else .1,
            "turnover_per_year": 5., "turnover": 1., "cost_fraction": .001,
            "cost_fraction_per_year": .005, "fills": 5, "cycles": 120,
            "ledger_digest": "good" if good else "base", "reconciliation_breaks": 0, "ledger_rejections": 0}


def harness(monkeypatch, table=None, meta=None, portfolio_bad=False):
    table, meta = (table, meta) if table is not None else market()
    calls = []
    monkeypatch.setattr(module, "load_snapshot", lambda _: (table, meta))
    monkeypatch.setattr(module, "evidence_of_edge", lambda *_: {"excess_t": 10., "alpha_t": 10.})
    monkeypatch.setattr(module, "research_metrics", lambda r: dict(r.metrics))

    def simulate(provider, metadata, sleeves, mode, start, end, run_id, **kwargs):
        if kwargs.get("check"):
            kwargs["check"]()
        assert int(provider.t.ts.max()) < meta["splits"]["test"][0]
        assert end <= meta["splits"]["validation"][1]
        good = any(v.family != StrategyFamily.BUY_HOLD for _, v, _ in sleeves)
        m = metric(good)
        if run_id == "validation-portfolio" and portfolio_bad:
            m["max_drawdown"] = .2
        # Differentiated development scores produce genuine feedback selection.
        if run_id.startswith("development-") and good:
            m["net_return"] += sum(sleeves[0][1].param_dict.values()) * 1e-6
        initial = 10_000_000_000
        rr = .0001 + (0.0001 if good else 0) + np.sin(np.arange(120)) * .001
        equity = (initial * np.cumprod(1 + rr)).astype(np.int64).tolist()
        calls.append((run_id, start, end, sleeves))
        return SimpleNamespace(metrics=m, initial_cash=initial, final_equity=int(initial * (1 + m["net_return"])),
                               equity=equity)

    monkeypatch.setattr(module, "simulate", simulate)
    return calls


def test_adaptive_rounds_are_bounded_and_all_experiments_recorded(monkeypatch, tmp_path):
    calls = harness(monkeypatch)
    reg = Registry(tmp_path / "registry.json")
    report = module.research(tmp_path, reg, 20)
    assert report["status"] == "complete"
    assert report["candidates_tested"] == len(report["experiments"]) <= 24
    assert {e["round"] for e in report["experiments"]} == {0, 1, 2}
    assert {e["family"] for e in report["experiments"]} >= {"breakout", "rsi_reversion", "ts_momentum"}
    assert any(e["parent"] and e["feedback"] for e in report["experiments"])
    assert all(e["split"] == "development" for e in report["experiments"])
    assert report["test_used_for_selection"] is False
    assert "holdout_views" in reg.data and not reg.data["holdout_views"]
    last_development = max(i for i, c in enumerate(calls) if c[0].startswith("development-"))
    first_validation = min(i for i, c in enumerate(calls) if c[0].startswith("walkforward-") or c[0].startswith("validation-"))
    assert last_development < first_validation
    assert all(r["accepted"] for r in report["roles"])
    assert sum(c for _, _, c in module.portfolio_sleeves(reg, ["SPY"])) == module.DEPLOYABLE
    assert module.research_current(reg, market()[1])
    assert len(Registry(reg.path).data["research_runs"]) == 1


def test_combined_portfolio_failure_prevents_all_promotions(monkeypatch, tmp_path):
    harness(monkeypatch, portfolio_bad=True)
    reg = Registry(tmp_path / "registry.json")
    report = module.research(tmp_path, reg, 20)
    assert report["status"] == "complete"
    assert not reg.champions()
    assert all("COMBINED_PORTFOLIO_GATE_FAILED" in r["reasons"] for r in report["roles"])
    assert len(module.portfolio_sleeves(reg, ["SPY"])) == 1


def test_test_changes_cannot_change_proposals_or_decisions(monkeypatch, tmp_path):
    table, meta = market()
    harness(monkeypatch, table, meta)
    a = module.research(tmp_path, Registry(tmp_path / "a.json"), 20)
    mask = table.ts >= meta["splits"]["test"][0]
    table.close[mask] *= 50
    table.high[mask] *= 50
    meta["content_sha256"] = table.content_hash()
    harness(monkeypatch, table, meta)
    b = module.research(tmp_path, Registry(tmp_path / "b.json"), 20)
    for key in ("proposal_summary", "experiments", "frozen_challengers", "roles", "portfolio_validation"):
        assert a[key] == b[key]


def test_validation_never_feeds_optimization_and_reuse_is_counted(monkeypatch, tmp_path):
    table, meta = market()
    harness(monkeypatch, table, meta)
    reg = Registry(tmp_path / "registry.json")
    a = module.research(tmp_path, reg, 20)
    b = module.research(tmp_path, reg, 20)
    assert a["experiments"] == b["experiments"]
    assert b["validation_views"] == 2
    assert b["validation_previously_inspected"]
    table.close[table.ts >= meta["splits"]["validation"][0]] *= 2
    harness(monkeypatch, table, meta)
    c = module.research(tmp_path, Registry(tmp_path / "c.json"), 20)
    assert a["experiments"] == c["experiments"]


def test_budget_stop_is_passive_and_cannot_leave_stale_champion(monkeypatch, tmp_path):
    harness(monkeypatch)
    reg = Registry(tmp_path / "registry.json")
    module.research(tmp_path, reg, 20)
    assert reg.champions()
    report = module.research(tmp_path, reg, 20, stop_requested=lambda: True)
    assert report["status"] == "stopped" and report["stop_reason"] == "STOP_REQUESTED"
    assert not reg.champions()
    assert [s for s, _, _ in module.portfolio_sleeves(reg, ["SPY"])] == ["core"]
    assert not module.research_current(reg, market()[1])


@pytest.mark.parametrize("change,reason", [({"net_return": .01}, "RETURN_NOT_ABOVE_BASELINE"),
    ({"sharpe": .9}, "SHARPE_NOT_ABOVE_BASELINE"), ({"sortino": 1.}, "SORTINO_NOT_ABOVE_BASELINE"),
    ({"max_drawdown": .2}, "DRAWDOWN_WORSE_THAN_BASELINE"),
    ({"turnover_per_year": 61}, "TURNOVER_TOO_HIGH"),
    ({"cost_fraction_per_year": .03}, "COST_BUDGET_EXCEEDED"),
    ({"cost_fraction": .1}, "NO_EXCESS_UNDER_DOUBLE_COSTS"),
    ({"sharpe": float("nan")}, "NONFINITE_METRICS"),
    ({"reconciliation_breaks": 1}, "EXECUTION_INTEGRITY_FAILURE")])
def test_every_metric_gate_is_enforced(change, reason):
    assert reason in module.metric_gates({**metric(True), **change}, metric())


def test_schema_universe_grid_and_bounds_reject_bad_model_proposals():
    raw = {"proposal_id": "p", "agent_id": "a", "family": "ts_momentum", "universe": ["SPY"],
           "param_grid": {"lookback": [100, 200], "threshold": [.01]}, "rationale": "trailing persistence"}
    assert len(validate_proposal(raw, "trend", ["SPY"])) == 2
    for change in ({"universe": ["BTC"]}, {"family": "buy_hold"},
                   {"param_grid": {"lookback": [9000], "threshold": [.01]}},
                   {"param_grid": {"lookback": [100] * 9, "threshold": [.01]}}):
        with pytest.raises(ValueError):
            validate_proposal({**raw, **change}, "trend", ["SPY"])


def test_bounded_grid_discards_only_relationally_invalid_combinations():
    raw = {"proposal_id": "p", "agent_id": "a", "family": "sma_crossover", "universe": ["SPY"],
           "param_grid": {"fast": [20, 100], "slow": [50, 200]}, "rationale": "trailing persistence"}
    proposals = validate_proposal(raw, "trend", ["SPY"])
    assert len(proposals) == 3
    assert all(p.version.param_dict["fast"] < p.version.param_dict["slow"] for p in proposals)
    with pytest.raises(ValueError, match="no valid combination"):
        validate_proposal({**raw, "param_grid": {"fast": [100], "slow": [50]}}, "trend", ["SPY"])
    # An invalid value must not be quietly hidden by otherwise useful combinations.
    with pytest.raises(ValueError, match="outside reviewed bounds"):
        validate_proposal({**raw, "param_grid": {"fast": [20, 900], "slow": [200]}}, "trend", ["SPY"])


def test_optimizer_feedback_changes_valid_versions():
    proposal = house_proposals("trend", ["SPY"], {"symbols": {"SPY": {"annualized_vol": .3}}})[0]
    children = mutations(proposal, {**metric(True), "turnover_per_year": 30}, metric(), 1)
    assert children and all(c.parent == proposal.version.version and "churn" in c.feedback for c in children)
    assert all(c.version.version != proposal.version.version for c in children)


@pytest.mark.parametrize("budget,rounds,candidates", [(0, 3, 8), (float("nan"), 3, 8), (20, 6, 8), (20, 3, 17)])
def test_invalid_resource_budgets_fail_before_loading(budget, rounds, candidates, tmp_path):
    with pytest.raises(ValueError):
        module.research(tmp_path, budget_s=budget, rounds=rounds, max_candidates=candidates)


def test_registry_excludes_concurrent_research_and_reloads_after_writer(tmp_path):
    from hellabandz.trading.registry import RegistryError
    path = tmp_path / "registry.json"
    writer, stale = Registry(path), Registry(path)
    with writer.research_lock():
        writer.data["validation_views"] = {"dataset": 3}
        writer._save()
        with pytest.raises(RegistryError, match="already running"):
            with stale.research_lock():
                pytest.fail("second writer entered exclusive research lock")
    with stale.research_lock():
        assert stale.data["validation_views"] == {"dataset": 3}


def test_cancel_during_live_research_does_not_initialize_economic_commands(monkeypatch, tmp_path):
    from hellabandz.trading import live
    table, meta = market()
    stopped = False
    monkeypatch.setattr(live, "load_snapshot", lambda _: (table, meta))
    monkeypatch.setattr(live, "Registry", lambda _: Registry(tmp_path / "registry.json"))
    monkeypatch.setattr(live, "ApiLedger", lambda *_: pytest.fail("ledger initialized after cancellation"))

    def cancel_research(*args, **kwargs):
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(live, "research", cancel_research)
    result = live.run_coordinated({"config": {}, "portfolio_id": "p", "run_id": "r"}, tmp_path,
                                  "http://unused", {}, lambda *_: None, lambda: stopped)
    assert result["stopped"] and result["cycles"] == 0


def test_model_failure_and_schema_repair_are_bounded_and_off_execution_path(monkeypatch):
    from hellabandz.agents import llm
    from hellabandz.trading.proposals import llm_proposals
    monkeypatch.setattr(llm, "available", lambda: True)
    raw = {"proposal_id": "p", "agent_id": "a", "family": "ts_momentum", "universe": ["SPY"],
           "param_grid": {"lookback": [100], "threshold": [.01]}, "rationale": "persistence"}
    calls = []

    def fake_model(system, user, **kwargs):
        calls.append(kwargs)
        assert "evidence MUST be an array" in system
        assert user.startswith('{"symbols": {}}') and "splits" not in user
        return {**raw, "evidence": "bad type" if len(calls) == 1 else ["development-only"]}, {"model": "mock"}

    monkeypatch.setattr(llm, "chat_json", fake_model)
    proposals, record = llm_proposals("trend", ["SPY"], {"symbols": {}}, 2.)
    assert proposals and record["source"] == "llm" and record["attempts"] == 2
    assert len(calls) == 2 and all(0 < c["timeout"] <= 2 and c["max_retries"] == 0 for c in calls)


def test_additive_model_seeds_do_not_remove_house_families_or_feedback(monkeypatch, tmp_path):
    from hellabandz.trading.proposals import Proposal
    from hellabandz.trading.strategies import make_version
    harness(monkeypatch)
    monkeypatch.setattr(module, "llm_proposals", lambda role, universe, *_: (
        [Proposal(make_version(role, "momentum", {"lookback": 150, "threshold": .02}, universe),
                  "llm-test", "bounded persistence")], {"role": role, "source": "mock"})
        if role == "trend" else ([], {"role": role, "source": "mock"}))
    report = module.research(tmp_path, Registry(tmp_path / "registry.json"), 20, use_llm=True)
    trend = [e for e in report["experiments"] if e["role"] == "trend"]
    assert {e["family"] for e in trend if e["round"] == 0} >= {"sma_crossover", "breakout", "ts_momentum"}
    assert any(e["source"] == "llm-test" for e in trend)
    assert any(e["round"] > 0 for e in trend)
    assert len(trend) <= 8


def test_fully_invested_spy_reference_is_separate_from_constrained_simulation():
    from hellabandz.backtester import SnapshotProvider
    from hellabandz.trading.evaluate import price_index_metrics
    table, meta = market()
    start, end = meta["splits"]["test"]
    report = price_index_metrics(SnapshotProvider(table), "SPY", start, end)
    prices = table.close[table.ts >= start]
    assert report["net_return"] == round(float(prices[-1] / prices[0] - 1), 6)
    assert report["mean_exposure_pct"] == 100 and report["max_drawdown"] == 0
    assert "no spread, fees" in report["methodology"]


def test_deadline_expiration_is_reported_without_promoting_partial_work(monkeypatch, tmp_path):
    harness(monkeypatch)
    tick = 0.

    def monotonic():
        nonlocal tick
        tick += .02
        return tick

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=monotonic))
    reg = Registry(tmp_path / "registry.json")
    report = module.research(tmp_path, reg, .1)
    assert report["status"] == "stopped" and report["stop_reason"] == "TIME_BUDGET_EXHAUSTED"
    assert not reg.champions() and not module.research_current(reg, market()[1])


def test_runtime_checks_cancellation_inside_simulation_cycles():
    from hellabandz.backtester import SnapshotProvider
    from hellabandz.trading.instruments import instruments_for_dataset
    from hellabandz.trading.marketdata import DataClass
    from hellabandz.trading.strategies import RuntimeStrategy
    table, meta = market()
    start, end = meta["splits"]["test"]
    rt = module.CoordinatedRuntime("deadline", module.VenueMode.HISTORICAL_SIM,
                                   [RuntimeStrategy(module.core_version(["SPY"]), "bar_sim")],
                                   {"core": module.DEPLOYABLE}, module.paper_budget(module.INITIAL),
                                   instruments_for_dataset(meta), DataClass.SYNTHETIC)
    rt.open()
    rt.warm(SnapshotProvider(table), start)

    def stop_after_two_cycles():
        if len(rt.records) >= 2:
            raise module.ResearchStopped("deadline inside simulation")

    with pytest.raises(module.ResearchStopped, match="inside simulation"):
        rt.run(SnapshotProvider(table), start, end, check=stop_after_two_cycles)
    assert len(rt.records) == 2


def test_managed_progress_never_binds_validation_boolean_to_integer_column():
    from hellabandz.execution.manager import adaptive_progress
    calls = []
    for stage, value in (("optimized", 8), ("validated", True), ("validated", False)):
        adaptive_progress(lambda *args: calls.append(args), stage, "trend", value)
    assert calls == [("adaptive:trend:optimized", 8), ("adaptive:trend:validated", None),
                     ("adaptive:trend:validated", None)]
    assert all(value is None or type(value) is int for _, value in calls)


def test_integration_polling_deadline_ignores_wall_clock_jumps(monkeypatch):
    import tests.conftest as helpers
    wall = iter([0., 1000.])
    stable = iter([0., .01, .02])
    outcomes = iter([False, True])
    monkeypatch.setattr(helpers, "time", SimpleNamespace(time=lambda: next(wall),
                        monotonic=lambda: next(stable), sleep=lambda _: None))
    assert helpers.wait_until(lambda: next(outcomes), timeout=1., every=0.)
