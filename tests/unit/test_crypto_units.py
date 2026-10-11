"""Versioned crypto snapshots preserve units through parsing, fills and accounting."""
import importlib.util
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hellabandz.contracts import ApplyBar, Bar, CancelOrder, OpenPortfolio, OrderRequest, SetAllocation, SubmitOrder
from hellabandz.ingestion.dataset import BarTable, load_snapshot, unit_context, write_snapshot
from hellabandz.ledger import EngineConfig, PortfolioState, check_invariants, process, replay

ROOT = Path(__file__).resolve().parents[2]
TS = datetime(2024, 1, 1, tzinfo=timezone.utc)


def parser():
    spec = importlib.util.spec_from_file_location("binance_units_test", ROOT / "scripts/ingest_binance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_binance_parser_prices_and_volume_share_one_fixed_lot(tmp_path):
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("source.csv", "1704067200000,43000,43001,42999,43000.123456,21.1679999\n")
    rows = parser().parse(path, "BTCUSDT")
    assert rows == [("BTCUSDT", 1704067200, 43_000_000, 43_001_000, 42_999_000, 43_000_123, 21_167)]
    table = BarTable.from_rows([tuple([*rows[0][:1], rows[0][1] + i * 60, *rows[0][2:]]) for i in range(10)])
    meta = {"dataset_id": "crypto-lot-v2", "source": "Binance spot", "schema_version": "bars-v2-lots",
            "unit_version": "base-lot-v2", "quote_currency": "USDT", "lots_per_base_unit": 1000,
            "base_units_per_lot": "0.001", "interval_s": 60}
    saved = write_snapshot(table, tmp_path / "snapshot", meta)
    loaded, manifest = load_snapshot(tmp_path / "snapshot")
    assert loaded.content_hash("bars-v2-lots") == saved["content_sha256"] == manifest["content_sha256"]
    assert loaded.content_hash() != saved["content_sha256"]
    with pytest.raises(ValueError, match="immutable"):
        write_snapshot(table, tmp_path / "snapshot", meta)
    for changes in [{"lots_per_base_unit": 1}, {"quote_currency": "USD"}, {"base_units_per_lot": "1"}, {"schema_version": "bars-v1"}]:
        (tmp_path / "snapshot/manifest.json").write_text(json.dumps({**manifest, **changes}))
        with pytest.raises(ValueError, match="base-lot-v2 requires"):
            load_snapshot(tmp_path / "snapshot")
    with pytest.raises(ValueError, match="supported USDT"):
        parser().parse(path, "BTCUSDC")
    legacy = tmp_path / "legacy"
    write_snapshot(table, legacy, {"dataset_id": "old", "source": "data.binance.vision", "interval_s": 60})
    with pytest.raises(ValueError, match="legacy Binance"):
        load_snapshot(legacy)


def crypto_execution(capital=100_000):
    state, cfg, events = PortfolioState(), EngineConfig(), []
    def send(command):
        emitted, response = process(state, command, cfg)
        events.extend(emitted)
        check_invariants(state)
        return response
    send(OpenPortfolio(initial_cash=capital * 1_000_000))
    send(SetAllocation(agent_id="a", capital=capital * 1_000_000))
    def bar(i, volume=21167, price=43_000_000):
        return ApplyBar(bar=Bar(symbol="BTCUSDT", ts=TS + timedelta(minutes=i), open=price, high=price, low=price,
                                close=price, volume=volume))
    send(bar(0))
    send(SubmitOrder(order=OrderRequest(client_order_id="buy", agent_id="a", symbol="BTCUSDT", side="BUY",
                                        qty=1000, signal_bar_ts=TS)))
    send(bar(1))
    return state, events, send, bar


def test_crypto_participation_cap_and_10k_affordability_are_repeatable():
    state, events, _, _ = crypto_execution()
    fill = next(e for e in events if e["type"] == "OrderFilled")
    assert fill["qty"] == 211
    assert fill["qty"] / 1000 <= 0.21167
    assert fill["fee"] == 211 * 5000
    assert state.cash == state.initial_cash - fill["qty"] * fill["px"] - fill["fee"]
    assert replay(events).digest() == state.digest() == crypto_execution()[0].digest()
    small, small_events, _, _ = crypto_execution(10000)
    assert 0 < small.position("a", "BTCUSDT").qty < 1000
    assert small.cash >= 0
    assert replay(small_events).digest() == small.digest()


def test_crypto_limit_partial_cancel_sell_and_accounting():
    state, events, send, bar = crypto_execution()
    assert state.orders["buy"].remaining > 0
    send(CancelOrder(client_order_id="buy", agent_id="a"))
    assert state.reserved_cash == 0
    send(SubmitOrder(order=OrderRequest(client_order_id="limit", agent_id="a", symbol="BTCUSDT", side="SELL",
                                        qty=50, order_type="LMT", limit_px=44_000_000, signal_bar_ts=TS + timedelta(minutes=1))))
    send(bar(2, price=43_000_000))
    assert state.orders["limit"].remaining == 50
    send(bar(3, volume=1000, price=44_000_000))
    assert state.orders["limit"].remaining == 40
    assert state.position("a", "BTCUSDT").qty == 201
    assert state.realized_pnl > 0 and state.fees_paid == 221 * 5000
    send(CancelOrder(client_order_id="limit", agent_id="a"))
    assert state.position("a", "BTCUSDT").reserved == 0
    assert state.digest() == replay(events).digest()


def test_crypto_human_facts_use_usdt_lots_and_short_statistics_are_explicit(monkeypatch):
    from hellabandz.agents import ask, session
    from hellabandz.agents.research import train_summary
    from hellabandz.backtester import SnapshotProvider
    units = unit_context({"unit_version": "base-lot-v2"})
    captured = {}
    monkeypatch.setattr(session.llm, "available", lambda: True)
    def chat(system, payload):
        captured.update(json.loads(payload))
        return {"action": "hold", "fraction": 0, "rationale": "test"}, {}
    monkeypatch.setattr(session.llm, "chat_json", chat)
    session.decide_crisis("momentum", {"BTCUSDT": 50}, 4000, {"BTCUSDT": 43}, units)
    assert captured["units"]["quote_currency"] == "USDT"
    assert captured["holdings_execution_lots"]["BTCUSDT"] == 50
    assert captured["prices_quote_per_execution_lot"]["BTCUSDT"] == 43
    assert "allocation_usd" not in captured
    facts = ask.facts({"units": units, "portfolio": {"state": {"cash": 10000000000}, "equity": 10000000000, "digest": "abc"}})
    answer = ask.deterministic_answer("cash and equity?", facts)
    assert "USDT 10,000.00" in answer and "$" not in answer
    table = BarTable.from_rows([("BTCUSDT", int(TS.timestamp()) + i * 60, 43000000, 43000000, 43000000, 43000000, 1000) for i in range(10)])
    summary = train_summary(SnapshotProvider(table), int(TS.timestamp()) + 600, {"unit_version": "base-lot-v2", "interval_s": 60})
    assert summary["symbols"]["BTCUSDT"]["frac_above_sma200"] is None
    assert summary["symbols"]["BTCUSDT"]["lag1_autocorr"] is None
    json.dumps(summary, allow_nan=False)
