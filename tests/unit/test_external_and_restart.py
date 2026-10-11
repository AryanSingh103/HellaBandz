"""External submissions with unknown outcomes, and coordinated runtime restart with working orders."""

from dataclasses import replace
from pathlib import Path

import pytest

from hellabandz.backtester import SnapshotProvider
from hellabandz.contracts import MICROS
from hellabandz.ingestion.dataset import load_snapshot
from hellabandz.trading.coordinator import paper_budget
from hellabandz.trading.execution import LiquidityConfig, VenueMode
from hellabandz.trading.external import ExternalAdapter, Journal, ProductionDisabled, RateLimiter
from hellabandz.trading.instruments import instruments_for_dataset
from hellabandz.trading.marketdata import DataClass
from hellabandz.trading.runtime import CoordinatedRuntime, checkpoint, restore
from hellabandz.trading.strategies import RuntimeStrategy, make_version


class FlakyVenue:
    def __init__(self):
        self.orders, self.timeout_next = {}, False

    def submit(self, coid, instrument, side, qty, limit_px):
        self.orders[coid] = {"accepted": True, "order_id": f"v-{coid}", "filled_qty": 0}
        if self.timeout_next:
            self.timeout_next = False
            raise TimeoutError("no response")  # the venue did receive it
        return self.orders[coid]

    def get_order(self, coid):
        return self.orders.get(coid)


def test_unknown_outcome_blocks_until_reconciled_and_survives_restart(tmp_path):
    venue, path = FlakyVenue(), tmp_path / "journal.jsonl"
    a = ExternalAdapter("demo-venue", venue, Journal(path), RateLimiter(100, 10))
    venue.timeout_next = True
    assert a.submit("c1", "SPY", "BUY", 1, 500 * MICROS)["status"] == "unknown"
    assert a.submit("c2", "SPY", "BUY", 1, 500 * MICROS)["reason"] == "RECONCILE_REQUIRED_AFTER_RESTART"
    b = ExternalAdapter("demo-venue", venue, Journal(path), RateLimiter(100, 10))  # process restarts
    assert not b.reconciled and b.submit("c2", "SPY", "BUY", 1, 500 * MICROS)["status"] == "refused"
    assert b.reconcile() == [{"client_order_id": "c1", "from": "UNKNOWN", "to": "ACKED"}]
    assert b.submit("c1", "SPY", "BUY", 1, 500 * MICROS)["reason"] == "CLIENT_ORDER_ID_ALREADY_USED"  # never resent
    assert b.submit("c2", "SPY", "BUY", 1, 500 * MICROS)["status"] == "acked" and len(venue.orders) == 2


def test_rate_limit_defers_and_production_is_disabled(tmp_path, monkeypatch):
    t = [0.0]
    a = ExternalAdapter("v", FlakyVenue(), Journal(tmp_path / "j"), RateLimiter(1, 1, clock=lambda: t[0]))
    assert a.submit("c1", "SPY", "BUY", 1, MICROS)["status"] == "acked"
    assert a.submit("c2", "QQQ", "BUY", 1, MICROS)["reason"] == "RATE_LIMITED"
    t[0] = 1.0
    assert a.submit("c2", "QQQ", "BUY", 1, MICROS)["status"] == "acked"
    with pytest.raises(ProductionDisabled):
        ExternalAdapter("v", FlakyVenue(), Journal(tmp_path / "p"), RateLimiter(1, 1), environment="production")
    monkeypatch.setenv("QS_PRODUCTION_ORDERS", "authorized")
    with pytest.raises(ProductionDisabled):
        ExternalAdapter("v", FlakyVenue(), Journal(tmp_path / "p"), RateLimiter(1, 1), environment="production")


def test_restart_with_working_orders_continues_identically():
    t, m = load_snapshot(Path(__file__).resolve().parents[2] / "data/fixtures/synthetic_1h")
    p = SnapshotProvider(t)
    te = m["splits"]["test"]
    U = ["AAPL", "MSFT", "NVDA", "QQQ", "SPY"]

    def fresh():
        strats = [RuntimeStrategy(make_version("trend", "trend", {"fast": 20, "slow": 100}, U), "paper_clob"),
                  RuntimeStrategy(make_version("core", "core", {}, U), "paper_clob")]
        rt = CoordinatedRuntime("RS", VenueMode.PAPER_CLOB, strats, {"trend": 4000 * MICROS, "core": 5000 * MICROS},
                                replace(paper_budget(10_000 * MICROS), tif="GTC", cooldown_cycles=0),
                                instruments_for_dataset(m), DataClass.SYNTHETIC,
                                liquidity=LiquidityConfig(participation_bps=0))  # one-lot ladder: orders stay working
        rt.open()
        rt.warm(p, te[0])
        return rt

    groups = []
    for b in p.bars(te[0], te[1]):
        if groups and groups[-1][0].ts == b.ts:
            groups[-1].append(b)
        else:
            groups.append([b])
    groups = groups[:120]
    whole = fresh()
    for g in groups:
        whole.step(g)
    part = fresh()
    cut = None
    for i, g in enumerate(groups):
        part.step(g)
        if any(o.agent_id == "exec" for o in part.ledger.state.orders.values()):
            cut = i + 1  # restart at the first point with working orders
            break
    assert cut is not None and cut < len(groups), "the scenario must have working orders at the restart point"
    resumed = restore(checkpoint(part))
    for g in groups[cut:]:
        resumed.step(g)
    assert resumed.ledger.state.digest() == whole.ledger.state.digest()
    assert resumed.authority.clob.head_hash == whole.authority.clob.head_hash
    assert resumed.authority.reconcile() == []
