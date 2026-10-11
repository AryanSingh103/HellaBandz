"""Passive core, basket signals, hold rebalancing and excess-over-core selection."""

import json
from pathlib import Path

import numpy as np
import pytest

from quantswarm.agents import tournament
from quantswarm.agents.tournament import DEPLOYABLE, MIN_FILLS, excess_series, information_ratio, pick_candidate, relative_score
from quantswarm.backtester import SnapshotProvider, Slot, rebalance_order, reentry_capital, run_backtest
from quantswarm.contracts import MICROS, StrategyFamily, StrategySpec
from quantswarm.ingestion.dataset import load_snapshot, write_snapshot
from quantswarm.ledger.state import OpenOrder, PortfolioState, Position, pos_key
from quantswarm.strategies import Series, basket_index, regime_reversion, reversion_speed, signal, universe_weights

FIXTURE = Path(__file__).resolve().parents[2] / "data/fixtures/synthetic_1h"
REGIME = {"regime_lookback": 60, "min_reversion": 0.02, "lookback": 30, "sell_z": 1.0, "rebuy_z": 0.0}


def series(close):
    close = np.asarray(close, dtype=np.float64)
    return Series(close, close, close)


def test_regime_overlay_never_sells_a_steady_trend():
    up = series(100 * np.exp(0.002 * np.arange(400)))
    assert (regime_reversion(up, **REGIME) == 10_000).all()


def test_regime_overlay_sells_strength_and_rebuys_in_a_range():
    t = np.arange(600)
    close = 100 + 5 * np.sin(2 * np.pi * t / 50)
    w = regime_reversion(series(close), **REGIME)
    settled = slice(100, None)
    assert 0 < (w[settled] == 0).mean() < 1
    # out of the market while price is high, back in while it is low
    assert close[settled][w[settled] == 0].mean() > close[settled][w[settled] > 0].mean()


def test_reversion_speed_separates_a_range_from_a_random_walk():
    rng = np.random.default_rng(5)
    shocks = rng.normal(0, 0.004, 4000)
    walk = np.cumsum(shocks)
    ou = np.zeros(4000)
    for i in range(1, 4000):
        ou[i] = ou[i - 1] - 0.1 * ou[i - 1] + shocks[i]
    speed_ou = np.nanmedian(reversion_speed(100 * np.exp(ou), 200))
    speed_walk = np.nanmedian(reversion_speed(100 * np.exp(walk), 200))
    assert 0.07 < speed_ou < 0.14 and speed_walk < 0.04


def test_basket_index_is_causal_and_ignores_scale():
    rng = np.random.default_rng(1)
    ts = np.arange(300, dtype=np.int64) * 3600
    a, b = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 300))), 20 * np.exp(np.cumsum(rng.normal(0, 0.02, 300)))
    full, full_ts = basket_index({"A": (series(a), ts), "B": (series(b), ts)})
    cut, _ = basket_index({"A": (series(a[:200]), ts[:200]), "B": (series(b[:200]), ts[:200])})
    assert np.array_equal(cut.close, full.close[:200]) and np.array_equal(full_ts, ts)
    scaled, _ = basket_index({"A": (series(a * 7), ts), "B": (series(b), ts)})
    assert np.allclose(scaled.close, full.close)


def test_basket_signal_is_shared_and_aligned_to_each_member():
    rng = np.random.default_rng(2)
    ts = np.arange(400, dtype=np.int64) * 3600
    a = 100 + 5 * np.sin(2 * np.pi * np.arange(400) / 50) + rng.normal(0, 0.1, 400)
    keep = np.ones(400, dtype=bool)
    keep[250:260] = False  # one member misses bars and starts later
    keep[:5] = False
    hist = {"A": (series(a), ts), "B": (series(a[keep] * 3), ts[keep])}
    w = universe_weights(StrategyFamily.REGIME_REVERSION, REGIME, hist)
    assert len(w["A"]) == 400 and len(w["B"]) == keep.sum()
    assert (w["A"][:5] == 0).all()  # no index before every member has traded
    assert np.array_equal(w["B"], w["A"][keep])
    # non-basket families still decide per symbol
    per_symbol = universe_weights(StrategyFamily.SMA_CROSSOVER, {"fast": 5, "slow": 20}, hist)
    assert np.array_equal(per_symbol["B"], signal(StrategyFamily.SMA_CROSSOVER, {"fast": 5, "slow": 20}, hist["B"][0]))


def held(qty, basis):
    s = PortfolioState()
    s.positions[pos_key("a", "SPY")] = Position(qty=qty, basis=basis)
    return s


def test_hold_slot_is_deployed_once_and_never_trimmed():
    slot, px = 1_000 * MICROS, 100 * MICROS
    assert rebalance_order(PortfolioState(), "a", "SPY", 10_000, slot, px, 1, hold=True) == ("BUY", 10)
    full = held(10, 1_000 * MICROS)
    assert rebalance_order(full, "a", "SPY", 10_000, slot, 2 * px, 1, hold=True) is None  # doubled: not trimmed
    assert rebalance_order(full, "a", "SPY", 10_000, slot, px // 2, 1, hold=True) is None  # halved: not topped up
    assert rebalance_order(full, "a", "SPY", 0, slot, 2 * px, 1, hold=True) == ("SELL", 10)
    # a value-targeting slot does trim the same position
    assert rebalance_order(full, "a", "SPY", 10_000, slot, 2 * px, 1) == ("SELL", 5)
    # with buying disabled an exit could never be reversed, so the hold slot stays put
    full.kill_switch = True
    assert rebalance_order(full, "a", "SPY", 0, slot, 2 * px, 1, hold=True) is None
    assert rebalance_order(full, "a", "SPY", 0, slot, 2 * px, 1) == ("SELL", 10)


def test_flat_hold_slot_is_resized_to_its_share_of_equity():
    spec = StrategySpec(strategy_id="r", agent_id="a", family="regime_reversion", params=REGIME, universe=["SPY"])
    flat = PortfolioState()
    assert reentry_capital(flat, spec, 4_000 * MICROS, 10_000 * MICROS, 11_000 * MICROS) == 4_400 * MICROS
    assert reentry_capital(flat, spec, 4_000 * MICROS, 10_000 * MICROS, 9_000 * MICROS) == 3_600 * MICROS
    assert reentry_capital(held(3, 300 * MICROS), spec, 4_000 * MICROS, 10_000 * MICROS, 11_000 * MICROS) is None
    working = PortfolioState()
    working.orders["o"] = OpenOrder(order_id="o", agent_id="a", symbol="SPY", side="BUY", qty=1, remaining=1, order_type="MKT",
                                    limit_px=None, reserved_cash=0, signal_ts=0, created_seq=1)
    assert reentry_capital(working, spec, 4_000 * MICROS, 10_000 * MICROS, 11_000 * MICROS) is None
    momentum = spec.model_copy(update={"family": StrategyFamily.SMA_CROSSOVER, "params": {"fast": 5, "slow": 20}})
    assert reentry_capital(flat, momentum, 4_000 * MICROS, 10_000 * MICROS, 11_000 * MICROS) is None


def test_core_backtest_buys_once_and_overlay_reinvests_gains():
    table, meta = load_snapshot(FIXTURE)
    p = SnapshotProvider(table)
    start, end = meta["splits"]["validation"]
    core = tournament.core_spec("T", meta["symbols"])
    r = run_backtest(p, [Slot(core, 9_000 * MICROS)], start, end, check_every_step=True)
    assert {f["side"] for f in r.fills} == {"BUY"} and r.rejections == 0
    assert max(f["bar_ts"] for f in r.fills) < start + 5 * 24 * 3600  # deployed in the first days, then untouched
    overlay = StrategySpec(strategy_id="o", agent_id="mean_reversion", family="regime_reversion",
                           params={**REGIME, "regime_lookback": 200, "lookback": 80}, universe=meta["symbols"])
    a = run_backtest(p, [Slot(overlay, 9_000 * MICROS)], start, end, check_every_step=True)
    b = run_backtest(p, [Slot(overlay, 9_000 * MICROS)], start, end)
    assert a.state_digest == b.state_digest and a.trades_digest == b.trades_digest
    assert {f["side"] for f in a.fills} == {"BUY", "SELL"}


def test_excess_statistics_and_candidate_choice():
    core = [100, 101, 102, 103, 104]
    assert information_ratio(excess_series(core, core)) == 0.0
    better = excess_series([100, 102, 103, 106, 107], core)
    assert better.sum() > 0 and information_ratio(better) > 0
    good = {"fills": [MIN_FILLS, MIN_FILLS], "excess": [np.array([0.01, 0.02, 0.0]), np.array([0.01, 0.0, 0.02])]}
    bad = {"fills": [MIN_FILLS, MIN_FILLS], "excess": [np.array([-0.01, -0.02, 0.0]), np.array([0.05, 0.06, 0.07])]}
    idle = {"fills": [0, 0], "excess": [np.array([0.5, 0.6, 0.7]), np.array([0.5, 0.6, 0.7])]}
    assert pick_candidate([bad, good, idle], [0])[0] == 1  # the idle candidate has too few fills to be chosen
    assert pick_candidate([bad, good, idle], [1])[0] == 0
    assert pick_candidate([idle, idle], [0, 1]) == (0, -1e9)


def test_evidence_needs_skill_and_not_just_less_market_exposure():
    from quantswarm.agents.tournament import evidence_of_edge
    rng = np.random.default_rng(11)
    falling = rng.normal(-0.0005, 0.005, 4000)
    half_invested = 0.5 * falling  # beats a falling core only by holding less of it
    low_beta = evidence_of_edge(half_invested, falling)
    assert low_beta["excess_t"] > 1 and abs(low_beta["alpha_t"]) < 1e-6 and low_beta["weight"] == 0.0
    skilled = evidence_of_edge(0.8 * falling + 0.0006 + rng.normal(0, 0.001, 4000), falling)
    assert skilled["alpha_t"] > 1 and skilled["excess_t"] > 1 and skilled["weight"] == 1.0
    assert 0.7 < skilled["beta"] < 0.9
    rising = -falling
    lagging = evidence_of_edge(0.5 * rising, rising)  # same low exposure in a rising market simply lags
    assert lagging["weight"] == 0.0
    assert evidence_of_edge(np.zeros(2), np.zeros(2))["weight"] == 0.0


def test_only_strategies_that_beat_the_core_out_of_sample_score():
    val = {"n_fills": 40, "max_drawdown": 0.10}
    assert relative_score(val, 0.8, 1.0, 0.12)[0] > 0
    assert relative_score(val, -0.2, 1.0, 0.12) == (0.0, ["no out-of-sample excess return over the passive core"])
    assert relative_score(val, 0.8, 0.0, 0.12)[0] == 0.0
    assert relative_score({**val, "n_fills": 3}, 0.8, 1.0, 0.12)[1] == [f"n_fills 3 < {MIN_FILLS}"]
    # a drawdown above the absolute limit is tolerated only when the passive core fell at least as far
    deep = {**val, "max_drawdown": 0.30}
    assert relative_score(deep, 0.8, 1.0, 0.12)[0] == 0.0
    assert relative_score(deep, 0.8, 1.0, 0.33)[0] > 0


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture missing")
def test_tournament_keeps_unearned_capital_in_the_core(tmp_path, monkeypatch):
    """With too little history for any strategy to earn capital, everything deployable sits in the core
    and the held-out portfolio is the passive basket."""
    table, _ = load_snapshot(FIXTURE)
    times = sorted(set(table.ts.tolist()))
    snapshot = tmp_path / "short"
    write_snapshot(table.select(table.ts < times[150]), snapshot,
                   {"dataset_id": "short-synthetic", "source": "synthetic test fixture", "interval_s": 3600})
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("QS_TOURNAMENT_PROCS", "1")
    monkeypatch.setattr("quantswarm.ingestion.registry.MANIFESTS", tmp_path / "manifests")
    m = tournament.run(snapshot, "Tcore")["manifest"]
    by_strategy = m["allocations"]["by_strategy"]
    core_id = m["core"]["spec"]["strategy_id"]
    assert len(m["fitted"]) == 3 and core_id not in {f["spec"]["strategy_id"] for f in m["fitted"]}
    assert abs(sum(a["weight"] for a in by_strategy.values()) - DEPLOYABLE) < 1e-6
    for r in m["ranked"]:
        assert (by_strategy[r["strategy_id"]]["weight"] > 0) == (r["score"] > 0)
        assert (r["score"] > 0) == (not r["gates"])
        assert len(r["fold_returns"]) == tournament.WALK_FORWARD_BLOCKS
    if not any(r["score"] > 0 for r in m["ranked"]):
        assert by_strategy[core_id]["weight"] == DEPLOYABLE
        assert m["held_out_test"]["portfolio"]["metrics"] == m["held_out_test"]["core_standalone"]
    json.loads((tmp_path / "manifests" / "Tcore.json").read_text())  # strict JSON on disk
